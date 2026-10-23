"""Organization-owned identity provisioning for interactive and LDAP batch flows.

This module is the concrete organization side of the SDK-free identity port. It
owns the organization aggregate and deliberately does not resolve or mutate a
canonical User, an external identity binding, or provider claims.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn, TypedDict, cast

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from core.identity_provisioning import (
    CustomerRoleName,
    ExternalIdentityProvisioningCommand,
    ExternalIdentityProvisioningResult,
    IdentityProvisioner,
    TenantRef,
    UserRef,
)
from core.mfa import PRIVILEGED_ROLE_NAMES, role_is_privileged
from organization.models import AssetHolder, Membership, Role, RoleGrant, RoleGrantScope, Tenant
from organization.services.role_permission_policy import permissions_for_sso_role
from users.models import GroupMembership, User

logger = logging.getLogger("itambox.organization.identity")

PRIVILEGED_JIT_TTL = timedelta(days=1)
LDAP_DIRECTORY_SYNC_REASON = "LDAP directory synchronization"
LDAP_DIRECTORY_SYNC_PRIVILEGED_TTL = timedelta(days=1)
LDAP_DIRECTORY_SYNC_MEMBER_PERMISSION_LIST = (
    "assets.view_asset",
    "assets.add_asset",
    "assets.change_asset",
    "inventory.view_accessory",
    "inventory.add_accessory",
    "inventory.change_accessory",
    "inventory.view_consumable",
    "inventory.add_consumable",
    "inventory.change_consumable",
    "inventory.view_kit",
    "inventory.add_kit",
    "inventory.change_kit",
    "inventory.view_component",
    "inventory.add_component",
    "inventory.change_component",
    "organization.view_location",
    "organization.add_location",
    "organization.change_location",
    "organization.view_site",
    "organization.add_site",
    "organization.change_site",
    "organization.view_assetholder",
    "organization.add_assetholder",
    "organization.change_assetholder",
    "extras.view_dashboard",
    "extras.add_dashboard",
    "extras.change_dashboard",
)
LDAP_DIRECTORY_SYNC_MEMBER_PERMISSIONS = frozenset(LDAP_DIRECTORY_SYNC_MEMBER_PERMISSION_LIST)


@dataclass(frozen=True)
class LDAPDirectoryIdentityCommand:
    """Normalized input for the non-interactive LDAP directory-sync API."""

    user: UserRef
    tenant: TenantRef


@dataclass(frozen=True)
class _TenantRow:
    pk: int
    deleted_at: datetime | None
    is_provider: bool
    managed_by_id: int | None


class _GrantMetadata(TypedDict):
    reason: str
    valid_until: datetime | None


@dataclass
class _AggregateLocks:
    memberships: dict[int, Membership]
    group_memberships: list[GroupMembership]
    grants: list[RoleGrant]
    grants_by_membership: dict[int, list[RoleGrant]]
    roles: list[Role]
    scopes: list[RoleGrantScope]
    scopes_by_grant: dict[int, list[RoleGrantScope]]


class IdentityProvisioningError(ValidationError):
    """Fail-closed organization aggregate error."""


# This hook is intentionally empty. It gives failure-injection tests a stable
# stage boundary without adding a runtime setting or a second writer.
def _stage_checkpoint(stage: str) -> None:
    del stage


def _reject(message: str) -> NoReturn:
    raise IdentityProvisioningError(message)


def _quoted(identifier: str) -> str:
    return connection.ops.quote_name(identifier)


def _tenant_row_from_db(row: object) -> _TenantRow:
    """Validate the DB-API tuple once before it enters typed domain state."""

    if not isinstance(row, tuple) or len(row) != 4:
        _reject("Identity provisioning tenant row has an invalid database shape.")
    pk, deleted_at, is_provider, managed_by_id = row
    if not isinstance(pk, int) or isinstance(pk, bool):
        _reject("Identity provisioning tenant row has an invalid primary key.")
    if deleted_at is not None and not isinstance(deleted_at, datetime):
        _reject("Identity provisioning tenant row has an invalid deletion timestamp.")
    if not isinstance(is_provider, bool):
        _reject("Identity provisioning tenant row has an invalid provider flag.")
    if managed_by_id is not None and (not isinstance(managed_by_id, int) or isinstance(managed_by_id, bool)):
        _reject("Identity provisioning tenant row has an invalid manager id.")
    return _TenantRow(
        pk=pk,
        deleted_at=deleted_at,
        is_provider=is_provider,
        managed_by_id=managed_by_id,
    )


def _lock_live_tenants(tenant_ids: set[int]) -> dict[int, _TenantRow]:
    """Lock the exact live tenant set once with parameterized PostgreSQL FOR SHARE."""

    ordered_ids = sorted({int(value) for value in tenant_ids})
    if not ordered_ids:
        _reject("Identity provisioning requires a tenant.")

    meta = Tenant._meta
    table = _quoted(meta.db_table)
    id_column = _quoted(str(meta.get_field("id").column))
    deleted_column = _quoted(str(meta.get_field("deleted_at").column))
    provider_column = _quoted(str(meta.get_field("is_provider").column))
    manager_column = _quoted(str(meta.get_field("managed_by").column))
    placeholders = ", ".join(["%s"] * len(ordered_ids))
    sql = (
        f"SELECT {id_column}, {deleted_column}, {provider_column}, {manager_column} "
        f"FROM {table} WHERE {id_column} IN ({placeholders}) "
        f"AND {deleted_column} IS NULL ORDER BY {id_column} FOR SHARE"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, ordered_ids)
        rows = cursor.fetchall()

    if len(rows) != len(ordered_ids):
        _reject("Identity provisioning target tenant is not live.")
    typed_rows = [_tenant_row_from_db(cast(object, row)) for row in rows]
    return {row.pk: row for row in typed_rows}


def _lock_existing_user(user: UserRef) -> User:
    user_id = getattr(user, "pk", None)
    if not isinstance(user_id, int):
        _reject("Identity provisioning requires an existing canonical user.")
    # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
    # service is its own authority
    locked = User._base_manager.select_for_update().filter(pk=user_id).first()
    if locked is None:
        _reject("Identity provisioning canonical user does not exist.")
    return locked


def _require_interactive_login_allowed(user: User) -> None:
    if user.can_login is False:
        _reject("Identity provisioning canonical user cannot log in.")


def _membership_tenant_ids(
    *,
    customer_row: _TenantRow,
    provider_intent: bool,
    sticky_provider: bool,
) -> set[int]:
    ids = {customer_row.pk}
    if (provider_intent or sticky_provider) and customer_row.managed_by_id is not None:
        # OIDC customer-mode sticky-provider dominance and provider transitions
        # both inspect the managing-provider Membership in this one lock query.
        ids.add(customer_row.managed_by_id)
    return ids


def _lock_memberships(user_id: int, tenant_ids: set[int]) -> dict[int, Membership]:
    # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
    # service is its own authority
    rows = list(
        Membership._base_manager.select_for_update(of=("self",))
        .filter(user_id=user_id, tenant_id__in=sorted(tenant_ids))
        .select_related("tenant")
        .order_by("tenant_id", "pk")
    )
    return {row.tenant_id: row for row in rows}


def _lock_group_memberships(membership_ids: set[int]) -> list[GroupMembership]:
    if not membership_ids:
        return []
    # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
    # service is its own authority
    return list(
        GroupMembership._base_manager.select_for_update()
        .filter(membership_id__in=sorted(membership_ids))
        .order_by("pk")
    )


def _lock_grants(membership_ids: set[int]) -> list[RoleGrant]:
    if not membership_ids:
        return []
    # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
    # service is its own authority
    return list(
        RoleGrant._base_manager.select_for_update().filter(membership_id__in=sorted(membership_ids)).order_by("pk")
    )


def _lock_roles(
    *,
    grants: list[RoleGrant],
    role_names: tuple[tuple[int, str], ...],
) -> list[Role]:
    predicate = Q(pk__in=sorted({grant.role_id for grant in grants}))
    for tenant_id, name in role_names:
        predicate |= Q(tenant_id=tenant_id, name=name, deleted_at__isnull=True)
    # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
    # service is its own authority
    return list(
        Role._base_manager.select_for_update().filter(predicate, deleted_at__isnull=True).order_by("tenant_id", "pk")
    )


def _lock_scopes(grants: list[RoleGrant]) -> list[RoleGrantScope]:
    grant_ids = sorted({grant.pk for grant in grants})
    if not grant_ids:
        return []
    # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
    # service is its own authority
    return list(
        RoleGrantScope._base_manager.select_for_update()
        .filter(role_grant_id__in=grant_ids)
        .order_by("role_grant_id", "pk")
    )


def _lock_aggregate(
    *,
    memberships: dict[int, Membership],
    customer_tenant_id: int,
    role_names: tuple[tuple[int, str], ...],
    provider_transition: bool,
) -> _AggregateLocks:
    membership_ids = {row.pk for row in memberships.values()}
    customer_membership = memberships.get(customer_tenant_id)
    group_membership_ids = {customer_membership.pk} if provider_transition and customer_membership else set()
    group_memberships = _lock_group_memberships(group_membership_ids)
    grants = _lock_grants(membership_ids)
    roles = _lock_roles(grants=grants, role_names=role_names)
    scopes = _lock_scopes(grants)
    grants_by_membership: dict[int, list[RoleGrant]] = {}
    for grant in grants:
        if grant.membership_id is not None:
            grants_by_membership.setdefault(grant.membership_id, []).append(grant)
    scopes_by_grant = _scopes_by_grant(scopes)
    return _AggregateLocks(
        memberships=memberships,
        group_memberships=group_memberships,
        grants=grants,
        grants_by_membership=grants_by_membership,
        roles=roles,
        scopes=scopes,
        scopes_by_grant=scopes_by_grant,
    )


def _reindex_aggregate_children(aggregate: _AggregateLocks) -> None:
    grants_by_membership: dict[int, list[RoleGrant]] = {}
    for grant in aggregate.grants:
        if grant.membership_id is not None:
            grants_by_membership.setdefault(grant.membership_id, []).append(grant)
    aggregate.grants_by_membership = grants_by_membership
    aggregate.scopes_by_grant = _scopes_by_grant(aggregate.scopes)


def _find_role(tenant_id: int, name: str, *, lock: bool = False) -> Role | None:
    # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
    # service is its own authority
    manager = Role._base_manager
    query = manager.filter(tenant_id=tenant_id, name=name, deleted_at__isnull=True).order_by("tenant_id", "pk")
    if lock:
        query = query.select_for_update()
    return query.first()


def _create_role(tenant_id: int, name: str, source: str) -> Role:
    permissions = permissions_for_sso_role(cast(CustomerRoleName, name))  # Permission is read only for a missing role.
    role: Role | None
    try:
        with transaction.atomic():
            # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant;
            # the service is its own authority
            role = Role._base_manager.create(
                tenant_id=tenant_id,
                name=name,
                description=f"Auto-provisioned {name} role via {source}",
                permissions=permissions,
            )
    except IntegrityError:
        role = _find_role(tenant_id, name, lock=True)
        if role is None:
            raise
        _log(
            "role_uniqueness_reconciled",
            source=source,
            tenant_id=tenant_id,
            role_id=role.pk,
            exception_type="IntegrityError",
        )
        return role
    _stage_checkpoint("customer.role_created")
    return role


def _resolve_customer_role(
    *,
    tenant_id: int,
    requested_name: str,
    source: str,
    locked_roles: list[Role],
) -> Role:
    by_name = {(role.tenant_id, role.name): role for role in locked_roles}
    role = by_name.get((tenant_id, requested_name))
    if role is not None:
        return role
    if requested_name in PRIVILEGED_ROLE_NAMES and not getattr(
        settings,
        "ITAMBOX_SSO_AUTOCREATE_PRIVILEGED_ROLES",
        True,
    ):
        _log("privileged_role_fallback", source=source, tenant_id=tenant_id)
        fallback = by_name.get((tenant_id, "Member"))
        return fallback if fallback is not None else _create_role(tenant_id, "Member", source)
    return _create_role(tenant_id, requested_name, source)


def _provider_relationship_valid(
    *,
    customer: _TenantRow,
    provider: _TenantRow,
    provider_id: int,
) -> bool:
    return (
        customer.pk != provider.pk
        and not customer.is_provider
        and provider.is_provider
        and provider.managed_by_id is None
        and customer.managed_by_id == provider_id
    )


def _profile_values(command: ExternalIdentityProvisioningCommand, user: User) -> tuple[str, str, str, str]:
    profile = command.profile
    source = profile.source.lower()
    email = (profile.email or getattr(user, "email", "") or "").strip()
    upn = (profile.upn or email or f"{user.username}@{source}").strip()
    first_name = profile.first_name or getattr(user, "first_name", "") or source.upper()
    last_name = profile.last_name or getattr(user, "last_name", "") or "User"
    return upn, email, first_name, last_name


def _holder_candidates(*, user_id: int, tenant_id: int, upn: str, email: str) -> list[AssetHolder]:
    predicate = Q(user_id=user_id) | Q(upn=upn)
    if email:
        predicate |= Q(email=email)
    # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
    # service is its own authority
    return list(
        AssetHolder._base_manager.select_for_update()
        .filter(tenant_id=tenant_id, deleted_at__isnull=True)
        .filter(predicate)
        .order_by("tenant_id", "pk")
    )


def _all_customer_holders(*, user_id: int, tenant_id: int) -> list[AssetHolder]:
    # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
    # service is its own authority
    return list(
        AssetHolder._base_manager.select_for_update()
        .filter(tenant_id=tenant_id, user_id=user_id)
        .order_by("tenant_id", "pk")
    )


def _holder_collision_re_read(*, user_id: int, tenant_id: int, upn: str, source: str) -> AssetHolder | None:
    """Constrained conflict recovery: user or exact UPN only, never email."""

    try:
        with transaction.atomic():
            # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant;
            # the service is its own authority
            rows = list(
                AssetHolder._base_manager.select_for_update()
                .filter(tenant_id=tenant_id, deleted_at__isnull=True)
                .filter(Q(user_id=user_id) | Q(upn=upn))
                .order_by("tenant_id", "pk")
            )
            same_user = next((row for row in rows if row.user_id == user_id), None)
            if same_user is not None:
                return same_user
            same_upn = next((row for row in rows if row.user_id is None and row.upn == upn), None)
            if same_upn is None:
                _log(
                    "holder_collision_unresolved",
                    source=source,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    exception_type="IntegrityError",
                )
                return None
            same_upn.user_id = user_id
            try:
                same_upn.save(update_fields=["user"])
            except IntegrityError:
                _log(
                    "holder_collision_unresolved",
                    source=source,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    exception_type="IntegrityError",
                )
                return None
            return same_upn
    except IntegrityError:
        _log(
            "holder_collision_unresolved",
            source=source,
            user_id=user_id,
            tenant_id=tenant_id,
            exception_type="IntegrityError",
        )
        return None


class _UnsafeHolderCandidate:
    """Marker for an unsafe email-only collision."""


_UNSAFE_HOLDER_CANDIDATE = _UnsafeHolderCandidate()


def _select_holder_candidate(
    *,
    user: User,
    tenant_id: int,
    upn: str,
    email: str,
    candidates: list[AssetHolder],
    source: str,
) -> AssetHolder | _UnsafeHolderCandidate | None:
    by_user = next((row for row in candidates if row.user_id == user.pk), None)
    if by_user is not None:
        return by_user

    by_upn = next((row for row in candidates if row.upn == upn), None)
    if by_upn is not None:
        if by_upn.user_id not in (None, user.pk):
            _log(
                "holder_collision_other_user",
                source=source,
                user_id=user.pk,
                tenant_id=tenant_id,
                holder_id=by_upn.pk,
            )
            return _UNSAFE_HOLDER_CANDIDATE
        return by_upn

    by_email = next((row for row in candidates if email and row.email == email), None)
    if by_email is None:
        return None
    if by_email.user_id not in (None, user.pk):
        _log(
            "holder_collision_other_user",
            source=source,
            user_id=user.pk,
            tenant_id=tenant_id,
            holder_id=by_email.pk,
        )
        return _UNSAFE_HOLDER_CANDIDATE
    if by_email.upn not in ("", upn):
        _log(
            "holder_email_hint_rejected",
            source=source,
            user_id=user.pk,
            tenant_id=tenant_id,
            holder_id=by_email.pk,
        )
        return _UNSAFE_HOLDER_CANDIDATE
    return by_email


def _identity_key(value: str | None) -> str:
    return (value or "").strip().casefold()


def login_may_act_as_holder(user: User, holder: AssetHolder) -> bool:
    """Whether an authenticated login is the person ``holder`` names.

    The read-only counterpart of ``_select_holder_candidate`` for one known
    holder: a linked login is authoritative; an unlinked holder matches only
    by UPN (the login's username), and by e-mail only when the holder carries
    no UPN, so a shared or recycled mailbox never stands in for a different
    UPN. A login already linked to another holder of the same tenant is that
    holder and never this one.
    """
    if user is None or not getattr(user, "is_authenticated", False) or holder is None:
        return False
    if holder.user_id is not None:
        return holder.user_id == user.pk
    same_tenant = Q(tenant__isnull=True) if holder.tenant_id is None else Q(tenant_id=holder.tenant_id)
    # unscoped: the signing login need not be a member of the holder's tenant; this is a read-only identity check
    linked_elsewhere = (
        AssetHolder._base_manager.filter(same_tenant, user_id=user.pk, deleted_at__isnull=True)
        .exclude(pk=holder.pk)
        .exists()
    )
    if linked_elsewhere:
        return False
    if holder.upn:
        login_upn = _identity_key(user.username)
        return bool(login_upn) and login_upn == _identity_key(holder.upn)
    login_email = _identity_key(user.email)
    return bool(login_email) and login_email == _identity_key(holder.email)


def _link_existing_holder(
    *,
    candidate: AssetHolder,
    user: User,
    tenant_id: int,
    upn: str,
    source: str,
) -> AssetHolder | None:
    try:
        with transaction.atomic():
            candidate.user_id = user.pk
            candidate.save(update_fields=["user"])
    except IntegrityError:
        return _holder_collision_re_read(user_id=user.pk, tenant_id=tenant_id, upn=upn, source=source)
    _stage_checkpoint("customer.holder_linked")
    return candidate


def _create_holder(
    *,
    user: User,
    tenant_id: int,
    upn: str,
    email: str,
    first_name: str,
    last_name: str,
    source: str,
) -> AssetHolder | None:
    holder: AssetHolder | None
    try:
        with transaction.atomic():
            # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant;
            # the service is its own authority
            holder = AssetHolder._base_manager.create(
                user_id=user.pk,
                tenant_id=tenant_id,
                upn=upn,
                email=email,
                first_name=first_name,
                last_name=last_name,
            )
    except IntegrityError:
        holder = _holder_collision_re_read(user_id=user.pk, tenant_id=tenant_id, upn=upn, source=source)
        if holder is None:
            _log(
                "holder_collision_unresolved",
                source=source,
                user_id=user.pk,
                tenant_id=tenant_id,
                exception_type="IntegrityError",
            )
        return holder
    _stage_checkpoint("customer.holder_created")
    return holder


@transaction.atomic
def link_or_create_holder(
    *,
    user: User,
    tenant_id: int,
    upn: str,
    email: str,
    first_name: str,
    last_name: str,
    candidates: list[AssetHolder] | None = None,
    source: str,
) -> AssetHolder | None:
    """Apply the shared exact-identity holder policy.

    Callers with an existing aggregate lock plan may pass its locked candidates;
    standalone callers load and lock the same user/UPN/email candidate set here.
    """
    if candidates is None:
        candidates = _holder_candidates(user_id=user.pk, tenant_id=tenant_id, upn=upn, email=email)
    candidate = _select_holder_candidate(
        user=user,
        tenant_id=tenant_id,
        upn=upn,
        email=email,
        candidates=candidates,
        source=source,
    )
    if isinstance(candidate, _UnsafeHolderCandidate):
        return None
    if candidate is not None and candidate.user_id == user.pk:
        return candidate
    if candidate is not None:
        return _link_existing_holder(
            candidate=candidate,
            user=user,
            tenant_id=tenant_id,
            upn=upn,
            source=source,
        )
    return _create_holder(
        user=user,
        tenant_id=tenant_id,
        upn=upn,
        email=email,
        first_name=first_name,
        last_name=last_name,
        source=source,
    )


def _scopes_by_grant(scopes: list[RoleGrantScope]) -> dict[int, list[RoleGrantScope]]:
    grouped: dict[int, list[RoleGrantScope]] = defaultdict(list)
    for scope in scopes:
        grouped[scope.role_grant_id].append(scope)
    return grouped


def _scope_key(scope: RoleGrantScope) -> tuple[str, int | None, int | None]:
    return scope.scope_type, scope.tenant_id, scope.tenant_group_id


def _delete_scope(scope: RoleGrantScope) -> None:
    scope.delete()


def _retire_conflicting_own_grants(
    grants: list[RoleGrant],
    role: Role,
    scopes_by_grant: dict[int, list[RoleGrantScope]],
) -> None:
    for grant in list(grants):
        grant_scopes = list(scopes_by_grant.get(grant.pk, ()))
        own_scopes = [scope for scope in grant_scopes if scope.scope_type == RoleGrantScope.SCOPE_OWN]
        if grant.role_id == role.pk:
            continue
        for scope in own_scopes:
            _delete_scope(scope)
        remaining = [scope for scope in grant_scopes if scope.scope_type != RoleGrantScope.SCOPE_OWN]
        scopes_by_grant[grant.pk] = remaining
        if not remaining:
            grant.delete()


def _grant_provenance_key(grant: RoleGrant) -> tuple[int | None, str]:
    """Return the immutable provenance boundary for same-role convergence."""

    return grant.granted_by_id, grant.reason


def _grant_validity_priority(grant: RoleGrant, now: datetime) -> tuple[int, datetime, int]:
    """Rank permanent, effective future, and expired grants conservatively.

    Permanent validity outranks every finite window. Among finite windows, an
    effective grant outranks an expired one and the latest window wins. The
    primary-key tie-break keeps a stable oldest row only when validity is equal.
    """

    if grant.valid_until is None:
        return 2, datetime.max.replace(tzinfo=now.tzinfo), -grant.pk
    if grant.valid_until > now:
        return 1, grant.valid_until, -grant.pk
    return 0, grant.valid_until, -grant.pk


def _grant_has_own_scope(grant: RoleGrant, scopes_by_grant: dict[int, list[RoleGrantScope]]) -> bool:
    return any(scope.scope_type == RoleGrantScope.SCOPE_OWN for scope in scopes_by_grant.get(grant.pk, ()))


def _grant_is_effective(grant: RoleGrant, now: datetime) -> bool:
    return grant.valid_until is None or grant.valid_until > now


def _scope_winners_for_group(
    group: list[RoleGrant],
    scopes_by_grant: dict[int, list[RoleGrantScope]],
    now: datetime,
) -> dict[tuple[str, int | None, int | None], RoleGrant]:
    priorities = {grant.pk: _grant_validity_priority(grant, now) for grant in group}
    winners: dict[tuple[str, int | None, int | None], RoleGrant] = {}
    for grant in group:
        for scope in scopes_by_grant.get(grant.pk, ()):
            key = _scope_key(scope)
            winner = winners.get(key)
            if winner is None or priorities[grant.pk] > priorities[winner.pk]:
                winners[key] = grant
    return winners


def _retain_group_after_scope_convergence(
    group: list[RoleGrant],
    scope_winners: dict[tuple[str, int | None, int | None], RoleGrant],
    scopes_by_grant: dict[int, list[RoleGrantScope]],
) -> list[RoleGrant]:
    retained_grants: list[RoleGrant] = []
    for grant in group:
        retained_scopes: list[RoleGrantScope] = []
        for scope in list(scopes_by_grant.get(grant.pk, ())):
            if scope_winners[_scope_key(scope)].pk == grant.pk:
                retained_scopes.append(scope)
            else:
                # The winner already has this semantic key and the widest
                # validity, so deleting this child cannot expand or shorten
                # the effective authorization for the key.
                _delete_scope(scope)
        scopes_by_grant[grant.pk] = retained_scopes
        if retained_scopes:
            retained_grants.append(grant)
        else:
            # Empty historical duplicates carry no semantic authorization;
            # their parent can be retired truthfully after its children were
            # handled above.
            grant.delete()
    return retained_grants


def _merge_duplicate_same_role_grants(
    grants: list[RoleGrant],
    role: Role,
    scopes_by_grant: dict[int, list[RoleGrantScope]],
) -> list[RoleGrant]:
    # Provenance remains the exact (granted_by_id, reason) pair. Each
    # semantic key independently keeps the grant with the widest validity.
    # Unique scopes stay on their original grant; no scope foreign key is
    # reassigned because expiry belongs to the parent RoleGrant.
    matching = [grant for grant in grants if grant.role_id == role.pk and grant.membership_id is not None]
    if not matching:
        return []
    now = timezone.now()
    groups: dict[tuple[int | None, str], list[RoleGrant]] = {}
    for grant in matching:
        groups.setdefault(_grant_provenance_key(grant), []).append(grant)
    retained_grants: list[RoleGrant] = []
    for group in groups.values():
        scope_winners = _scope_winners_for_group(group, scopes_by_grant, now)
        retained_grants.extend(_retain_group_after_scope_convergence(group, scope_winners, scopes_by_grant))
    return retained_grants


def _grant_metadata(role: Role, source: str) -> _GrantMetadata:
    if not role_is_privileged(role):
        return {"reason": "", "valid_until": None}
    return {
        "reason": f"{source} group-claim provisioning",
        "valid_until": timezone.now() + PRIVILEGED_JIT_TTL,
    }


def _ensure_own_scope(grant: RoleGrant, existing_scopes: list[RoleGrantScope] | None = None) -> None:
    if existing_scopes is not None:
        if any(
            scope.role_grant_id == grant.pk and scope.scope_type == RoleGrantScope.SCOPE_OWN
            for scope in existing_scopes
        ):
            return
    else:
        # Callers outside the ordinary aggregate lock path may ask for a direct
        # exact lookup. The interactive path always supplies the captured rows.
        # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
        # service is its own authority
        if RoleGrantScope._base_manager.filter(
            role_grant_id=grant.pk,
            scope_type=RoleGrantScope.SCOPE_OWN,
        ).exists():
            return
    try:
        with transaction.atomic():
            # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant;
            # the service is its own authority
            RoleGrantScope._base_manager.create(
                role_grant_id=grant.pk,
                scope_type=RoleGrantScope.SCOPE_OWN,
            )
    except IntegrityError:
        # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
        # service is its own authority
        if not RoleGrantScope._base_manager.filter(
            role_grant_id=grant.pk,
            scope_type=RoleGrantScope.SCOPE_OWN,
        ).exists():
            raise


def _refresh_grant_validity(grant: RoleGrant, desired_until: datetime | None) -> None:
    # Only lengthen a finite window or convert a finite non-privileged window
    # to permanent; never shorten an existing validity.
    if desired_until is None:
        if grant.valid_until is None:
            return
    elif grant.valid_until is None or grant.valid_until >= desired_until:
        return
    grant.valid_until = desired_until
    grant.save(update_fields=["valid_until"])


def _refresh_system_grant_metadata(grant: RoleGrant, metadata: _GrantMetadata) -> None:
    """Refresh only the current system provenance, never a manual row.

    A refresh can extend a finite system window or make a non-privileged system
    grant permanent. It never shortens a permanent/future window and never
    changes a row whose provenance is operator/manual/historical.
    """

    if grant.granted_by_id is not None or grant.reason != metadata["reason"]:
        return
    _refresh_grant_validity(grant, metadata["valid_until"])


def _reconcile_customer_grants(
    *,
    membership: Membership,
    role: Role,
    grants: list[RoleGrant],
    scopes: list[RoleGrantScope],
    source: str,
) -> RoleGrant:
    membership_grants = [grant for grant in grants if grant.membership_id == membership.pk]
    membership_grant_ids = {grant.pk for grant in membership_grants}
    membership_scopes = [scope for scope in scopes if scope.role_grant_id in membership_grant_ids]
    scopes_by_grant = _scopes_by_grant(membership_scopes)
    _retire_conflicting_own_grants(membership_grants, role, scopes_by_grant)
    canonicals = _merge_duplicate_same_role_grants(membership_grants, role, scopes_by_grant)
    metadata = _grant_metadata(role, source)
    desired_key = (None, metadata["reason"])
    desired = next(
        (grant for grant in canonicals if _grant_provenance_key(grant) == desired_key),
        None,
    )
    now = timezone.now()
    effective_own = [
        grant
        for grant in canonicals
        if _grant_has_own_scope(grant, scopes_by_grant) and _grant_is_effective(grant, now)
    ]
    if effective_own:
        current = max(effective_own, key=lambda grant: _grant_validity_priority(grant, now))
        _refresh_system_grant_metadata(current, metadata)
    elif desired is not None:
        current = desired
        _refresh_system_grant_metadata(current, metadata)
        _ensure_own_scope(current, existing_scopes=scopes_by_grant.get(current.pk, []))
    else:
        # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
        # service is its own authority
        current = RoleGrant._base_manager.create(
            membership_id=membership.pk,
            role_id=role.pk,
            granted_by=None,
            **metadata,
        )
        _ensure_own_scope(current, existing_scopes=[])
    _stage_checkpoint("customer.scope_reconciled")
    _stage_checkpoint("customer.grant_reconciled")
    return current


def _active_provider_membership(*, customer_row: _TenantRow, memberships: dict[int, Membership]) -> Membership | None:
    provider_id = customer_row.managed_by_id
    if provider_id is None:
        return None
    membership = memberships.get(provider_id)
    if membership is None or not membership.is_active:
        return None
    provider = membership.tenant
    if provider.deleted_at is not None or not provider.is_provider or provider.managed_by_id is not None:
        return None
    return membership


def _validate_command_tenants(
    command: ExternalIdentityProvisioningCommand,
) -> tuple[int, int | None, dict[int, _TenantRow]]:
    customer_id = getattr(command.customer_tenant, "pk", None)
    if not isinstance(customer_id, int):
        _reject("Identity provisioning requires an existing customer tenant.")
    provider_intent = command.provider_staff
    provider_id = getattr(provider_intent.provider_tenant, "pk", None) if provider_intent else None
    if provider_intent is not None and not isinstance(provider_id, int):
        _reject("Identity provisioning requires an existing provider tenant.")
    tenant_ids = {customer_id}
    if provider_id is not None:
        tenant_ids.add(provider_id)
    rows = _lock_live_tenants(tenant_ids)
    customer = rows[customer_id]
    if provider_id is not None and not _provider_relationship_valid(
        customer=customer,
        provider=rows[provider_id],
        provider_id=provider_id,
    ):
        _reject("Identity provisioning provider relationship is invalid.")
    return customer_id, provider_id, rows


def _provider_transition(
    *,
    command: ExternalIdentityProvisioningCommand,
    customer_id: int,
    provider_id: int,
    rows: dict[int, _TenantRow],
    aggregate: _AggregateLocks,
    locked_user: User,
) -> ExternalIdentityProvisioningResult:
    intent = command.provider_staff
    assert intent is not None
    provider_roles = [
        role for role in aggregate.roles if role.tenant_id == provider_id and role.name == intent.role_name
    ]
    provider_role = provider_roles[0] if provider_roles else None
    if provider_role is None:
        _log(
            "provider_mapping_rejected",
            source=command.profile.source,
            user_id=locked_user.pk,
            customer_tenant_id=customer_id,
            provider_tenant_id=provider_id,
        )
        return ExternalIdentityProvisioningResult(mode="provider_mapping_rejected")

    customer_membership = aggregate.memberships.get(customer_id)
    provider_membership = aggregate.memberships.get(provider_id)
    customer_holders = _all_customer_holders(user_id=locked_user.pk, tenant_id=customer_id)

    if provider_membership is None:
        provider_membership, created = _create_membership(
            user_id=locked_user.pk,
            tenant_id=provider_id,
        )
        if not created:
            refreshed_grants = _lock_grants({provider_membership.pk})
            aggregate.grants = [
                grant for grant in aggregate.grants if grant.membership_id != provider_membership.pk
            ] + refreshed_grants
            refreshed_scopes = _lock_scopes(refreshed_grants)
            refreshed_grant_ids = {grant.pk for grant in refreshed_grants}
            aggregate.scopes = [
                scope for scope in aggregate.scopes if scope.role_grant_id not in refreshed_grant_ids
            ] + refreshed_scopes
            _reindex_aggregate_children(aggregate)
    if not provider_membership.is_active:
        provider_membership.is_active = True
        provider_membership.save(update_fields=["is_active"])
    aggregate.memberships[provider_id] = provider_membership
    _stage_checkpoint("provider.membership_activated")

    # Provider mapping never mints a grant, but an existing operator/SCIM/
    # manual/historical provider grant is independent durable authorization and
    # must remain byte/audit equivalent while the Membership is reactivated.

    if customer_membership is not None:
        customer_membership.delete()
    _stage_checkpoint("provider.customer_retired")

    for holder in customer_holders:
        holder.user_id = None
        holder.save(update_fields=["user"])
    _stage_checkpoint("provider.holders_unlinked")

    return ExternalIdentityProvisioningResult(
        mode="provider_staff",
        membership_id=provider_membership.pk,
        role_id=provider_role.pk,
    )


def _requested_role_names(
    *,
    command: ExternalIdentityProvisioningCommand,
    customer_id: int,
    provider_id: int | None,
) -> tuple[tuple[int, str], ...]:
    if provider_id is not None:
        intent = command.provider_staff
        if intent is None:
            _reject("Identity provisioning provider intent is missing.")
        return ((provider_id, intent.role_name),)
    names = [(customer_id, command.customer_role_name)]
    if command.customer_role_name in PRIVILEGED_ROLE_NAMES and not getattr(
        settings, "ITAMBOX_SSO_AUTOCREATE_PRIVILEGED_ROLES", True
    ):
        names.append((customer_id, "Member"))
    return tuple(names)


def _create_membership(*, user_id: int, tenant_id: int) -> tuple[Membership, bool]:
    """Create a Membership or reread the exact live uniqueness identity."""

    try:
        with transaction.atomic():
            membership = Membership(
                user_id=user_id,
                tenant_id=tenant_id,
                is_active=True,
            )
            membership.__dict__["_skip_asset_holder_autolink"] = True
            membership.save(force_insert=True)
            return membership, True
    except IntegrityError:
        # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
        # service is its own authority
        existing_membership = (
            Membership._base_manager.select_for_update().filter(user_id=user_id, tenant_id=tenant_id).first()
        )
        if existing_membership is None:
            raise
        return existing_membership, False


def _provision_customer(
    *,
    command: ExternalIdentityProvisioningCommand,
    customer_id: int,
    locked_user: User,
    memberships: dict[int, Membership],
    aggregate: _AggregateLocks,
) -> ExternalIdentityProvisioningResult:
    source = command.profile.source
    role = _resolve_customer_role(
        tenant_id=customer_id,
        requested_name=command.customer_role_name,
        source=source,
        locked_roles=aggregate.roles,
    )
    upn, email, first_name, last_name = _profile_values(command, locked_user)
    candidates = _holder_candidates(
        user_id=locked_user.pk,
        tenant_id=customer_id,
        upn=upn,
        email=email,
    )
    membership = memberships.get(customer_id)
    if membership is None:
        membership, created = _create_membership(user_id=locked_user.pk, tenant_id=customer_id)
        if not created:
            # A competing Membership writer may also have committed grants.
            # Refresh only the conflict path; normal provisioning keeps one query per lock table.
            refreshed_grants = _lock_grants({membership.pk})
            aggregate.grants = [
                grant for grant in aggregate.grants if grant.membership_id != membership.pk
            ] + refreshed_grants
            refreshed_scopes = _lock_scopes(refreshed_grants)
            refreshed_grant_ids = {grant.pk for grant in refreshed_grants}
            aggregate.scopes = [
                scope for scope in aggregate.scopes if scope.role_grant_id not in refreshed_grant_ids
            ] + refreshed_scopes
            refreshed_roles = _lock_roles(
                grants=refreshed_grants,
                role_names=((customer_id, role.name),),
            )
            roles_by_pk = {locked_role.pk: locked_role for locked_role in aggregate.roles}
            roles_by_pk.update({locked_role.pk: locked_role for locked_role in refreshed_roles})
            aggregate.roles = sorted(
                roles_by_pk.values(), key=lambda locked_role: (locked_role.tenant_id, locked_role.pk)
            )
            _reindex_aggregate_children(aggregate)
        memberships[customer_id] = membership
        aggregate.memberships = memberships
        _stage_checkpoint("customer.membership_created")

    customer_grants = aggregate.grants_by_membership.get(membership.pk, [])
    customer_grant_ids = {grant.pk for grant in customer_grants}
    customer_scopes = [
        scope
        for scoped_rows in aggregate.scopes_by_grant.values()
        for scope in scoped_rows
        if scope.role_grant_id in customer_grant_ids
    ]

    holder = link_or_create_holder(
        user=locked_user,
        tenant_id=customer_id,
        upn=upn,
        email=email,
        first_name=first_name,
        last_name=last_name,
        candidates=candidates,
        source=source,
    )
    grant = _reconcile_customer_grants(
        membership=membership,
        role=role,
        grants=customer_grants,
        scopes=customer_scopes,
        source=source,
    )
    return ExternalIdentityProvisioningResult(
        mode="customer",
        holder_id=holder.pk if holder is not None else None,
        membership_id=membership.pk,
        role_id=grant.role_id,
    )


class OrganizationIdentityProvisioner(IdentityProvisioner):
    """The sole interactive organization aggregate writer."""

    def provision(self, command: ExternalIdentityProvisioningCommand) -> ExternalIdentityProvisioningResult:
        """Run the normative Tenant FOR SHARE -> User -> aggregate lock plan.

        The caller may hold an OIDC binding-row lock in the surrounding Phase-B
        transaction, but must not pre-lock User or Tenant and must not request a
        reversed or skipped service lock. Organization owns this handoff.
        """
        with transaction.atomic():
            customer_id, provider_id, tenant_rows = _validate_command_tenants(command)
            customer_row = tenant_rows[customer_id]
            locked_user = _lock_existing_user(command.user)
            _require_interactive_login_allowed(locked_user)
            _stage_checkpoint("locks.user")
            membership_ids = _membership_tenant_ids(
                customer_row=customer_row,
                provider_intent=provider_id is not None,
                sticky_provider=provider_id is None and command.profile.source == "OIDC",
            )
            memberships = _lock_memberships(locked_user.pk, membership_ids)

            if provider_id is None and command.profile.source == "OIDC":
                sticky = _active_provider_membership(customer_row=customer_row, memberships=memberships)
                if sticky is not None:
                    return ExternalIdentityProvisioningResult(mode="provider_staff", membership_id=sticky.pk)

            role_names = _requested_role_names(
                command=command,
                customer_id=customer_id,
                provider_id=provider_id,
            )
            aggregate = _lock_aggregate(
                memberships=memberships,
                customer_tenant_id=customer_id,
                role_names=role_names,
                provider_transition=provider_id is not None,
            )
            if provider_id is not None:
                return _provider_transition(
                    command=command,
                    customer_id=customer_id,
                    provider_id=provider_id,
                    rows=tenant_rows,
                    aggregate=aggregate,
                    locked_user=locked_user,
                )
            return _provision_customer(
                command=command,
                customer_id=customer_id,
                locked_user=locked_user,
                memberships=memberships,
                aggregate=aggregate,
            )


def _get_or_create_directory_role(*, tenant_id: int, locked_roles: list[Role]) -> Role:
    """Resolve the already-locked role name, creating only after that pass."""

    role = next(
        (
            locked_role
            for locked_role in locked_roles
            if locked_role.tenant_id == tenant_id and locked_role.name == "Member"
        ),
        None,
    )
    if role is not None:
        return role
    try:
        with transaction.atomic():
            # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant;
            # the service is its own authority
            role = Role._base_manager.create(
                tenant_id=tenant_id,
                name="Member",
                description="Default Standard Member",
                permissions=list(LDAP_DIRECTORY_SYNC_MEMBER_PERMISSION_LIST),
            )
    except IntegrityError:
        role = _find_role(tenant_id, "Member", lock=True)
        if role is None:
            raise
        return role
    _stage_checkpoint("ldap.role_created")
    return role


def _ensure_directory_grant(
    *,
    membership: Membership,
    role: Role,
    existing_grants: list[RoleGrant],
    existing_scopes: list[RoleGrantScope],
) -> RoleGrant | None:
    now = timezone.now()
    owned = [
        grant
        for grant in existing_grants
        if grant.membership_id == membership.pk
        and grant.role_id == role.pk
        and grant.reason == LDAP_DIRECTORY_SYNC_REASON
        and grant.granted_by_id is None
    ]
    scopes_by_grant = _scopes_by_grant(existing_scopes)
    if len(owned) == 1:
        grant = owned[0]
        desired_until = now + LDAP_DIRECTORY_SYNC_PRIVILEGED_TTL if role_is_privileged(role) else None
        _refresh_grant_validity(grant, desired_until)
        if not any(scope.scope_type == RoleGrantScope.SCOPE_OWN for scope in scopes_by_grant.get(grant.pk, ())):
            _ensure_own_scope(grant, existing_scopes=existing_scopes)
        _stage_checkpoint("ldap.grant_refreshed")
        return grant

    # Ambiguous LDAP-owned rows are historical evidence, not a merge candidate.
    # Leave every such row/scope untouched, then inspect all same-role OWN rows
    # for an effective equivalent before creating one fresh exact LDAP row.
    active_equivalent = next(
        (
            grant
            for grant in existing_grants
            if grant.membership_id == membership.pk
            and grant.role_id == role.pk
            and any(scope.scope_type == RoleGrantScope.SCOPE_OWN for scope in scopes_by_grant.get(grant.pk, ()))
            and (grant.valid_until is None or grant.valid_until > now)
        ),
        None,
    )
    if active_equivalent is not None:
        return active_equivalent

    desired_until = now + LDAP_DIRECTORY_SYNC_PRIVILEGED_TTL if role_is_privileged(role) else None
    try:
        with transaction.atomic():
            # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant;
            # the service is its own authority
            grant = RoleGrant._base_manager.create(
                membership_id=membership.pk,
                role_id=role.pk,
                reason=LDAP_DIRECTORY_SYNC_REASON,
                valid_until=desired_until,
                granted_by_id=None,
            )
    except IntegrityError:
        # unscoped: provisioning locks and writes rows regardless of soft-delete state and active tenant; the
        # service is its own authority
        exact = (
            RoleGrant._base_manager.select_for_update()
            .filter(
                membership_id=membership.pk,
                role_id=role.pk,
                reason=LDAP_DIRECTORY_SYNC_REASON,
                granted_by__isnull=True,
            )
            .order_by("pk")
            .first()
        )
        if exact is None:
            raise
        _ensure_own_scope(exact)
        return exact
    _ensure_own_scope(grant, existing_scopes=existing_scopes)
    _stage_checkpoint("ldap.grant_created")
    return grant


def provision_ldap_directory_identity(command: LDAPDirectoryIdentityCommand) -> None:
    """Persist the fixed LDAP directory-sync Member contract.

    This API is intentionally separate from the interactive port: it never
    creates or links an AssetHolder and it never consumes the interactive SSO
    permission catalog.
    """

    tenant_id = getattr(command.tenant, "pk", None)
    user_id = getattr(command.user, "pk", None)
    if not isinstance(tenant_id, int) or not isinstance(user_id, int):
        _reject("LDAP directory synchronization requires existing rows.")

    with transaction.atomic():
        _lock_live_tenants({tenant_id})
        locked_user = _lock_existing_user(command.user)
        memberships = _lock_memberships(locked_user.pk, {tenant_id})
        existing_membership = memberships.get(tenant_id)
        aggregate = _lock_aggregate(
            memberships=memberships,
            customer_tenant_id=tenant_id,
            role_names=((tenant_id, "Member"),),
            provider_transition=False,
        )
        role = _get_or_create_directory_role(tenant_id=tenant_id, locked_roles=aggregate.roles)
        if existing_membership is None:
            existing_membership, created = _create_membership(user_id=locked_user.pk, tenant_id=int(tenant_id))
            if not created:
                refreshed_grants = _lock_grants({existing_membership.pk})
                aggregate.grants = [
                    grant for grant in aggregate.grants if grant.membership_id != existing_membership.pk
                ] + refreshed_grants
                refreshed_scopes = _lock_scopes(refreshed_grants)
                refreshed_grant_ids = {grant.pk for grant in refreshed_grants}
                aggregate.scopes = [
                    scope for scope in aggregate.scopes if scope.role_grant_id not in refreshed_grant_ids
                ] + refreshed_scopes
                _reindex_aggregate_children(aggregate)
            _stage_checkpoint("ldap.membership_created")
        existing_grants = aggregate.grants_by_membership.get(existing_membership.pk, [])
        existing_grant_ids = {grant.pk for grant in existing_grants}
        existing_scopes = [
            scope
            for scope_list in aggregate.scopes_by_grant.values()
            for scope in scope_list
            if scope.role_grant_id in existing_grant_ids
        ]
        _ensure_directory_grant(
            membership=existing_membership,
            role=role,
            existing_grants=existing_grants,
            existing_scopes=existing_scopes,
        )


organization_identity_provisioner: IdentityProvisioner = OrganizationIdentityProvisioner()


_ALLOWED_LOG_FIELDS = frozenset(
    {
        "source",
        "reason_code",
        "user_id",
        "customer_tenant_id",
        "provider_tenant_id",
        "tenant_id",
        "role_id",
        "holder_id",
        "membership_id",
        "exception_type",
    }
)


def _log(reason_code: str, **fields: str | int | None) -> None:
    safe: dict[str, str | int | None] = {key: value for key, value in fields.items() if key in _ALLOWED_LOG_FIELDS}
    safe["reason_code"] = reason_code
    logger.warning("identity provisioning event", extra=safe)


__all__ = [
    "IdentityProvisioningError",
    "OrganizationIdentityProvisioner",
    "organization_identity_provisioner",
    "LDAPDirectoryIdentityCommand",
    "provision_ldap_directory_identity",
    "LDAP_DIRECTORY_SYNC_REASON",
    "LDAP_DIRECTORY_SYNC_MEMBER_PERMISSION_LIST",
    "LDAP_DIRECTORY_SYNC_MEMBER_PERMISSIONS",
]

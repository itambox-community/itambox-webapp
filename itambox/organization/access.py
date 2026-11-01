"""Canonical tenant, RBAC, and explicitly shared-resource access helpers."""

import datetime
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional

from django.db.models import Q
from django.utils import timezone

from core import context as core_context
from core import tenant_scope as _tenant_scope
from core.authorization_cache import synchronize_authorization_cache
from core.context import SystemAuthorizationContext, get_current_request_id
from organization import rbac as _rbac
from organization.models import TenantResourceGrant

# Re-exported for established importers.
_descendant_group_ids_cache = _tenant_scope._descendant_group_ids_cache


def get_descendant_tenant_group_ids(group_id: int | None, live_only: bool = False) -> set[int]:
    """Kernel-owned descendant-group walk (issue #100); kept as a typed wrapper
    so the public organization API surface remains a real definition."""
    return _tenant_scope.get_descendant_tenant_group_ids(group_id, live_only=live_only)


def get_ancestor_tenant_group_ids(group_id: int | None, live_only: bool = False) -> set[int]:
    """Kernel-owned ancestor-group walk (issue #100); kept as a typed wrapper
    so the public organization API surface remains a real definition."""
    return _tenant_scope.get_ancestor_tenant_group_ids(group_id, live_only=live_only)


def shared_resource_ids(model: type[object], tenant: object | None) -> Iterable[int]:
    """Pool ids of ``model`` explicitly shared to ``tenant``."""
    # inline imports: app-registry: ContentType and organization models are not
    # loadable while Django is still loading apps, and this module is imported
    # during app setup (core.auth, core.managers, itambox.middleware).
    from django.contrib.contenttypes.models import ContentType

    if tenant is None:
        return TenantResourceGrant.objects.none().values_list("resource_id", flat=True)
    content_type = ContentType.objects.get_for_model(model)
    grantee = Q(grantee_tenant_id=tenant.pk)
    ancestor_group_ids = get_ancestor_tenant_group_ids(
        tenant.group_id,
        live_only=True,
    )
    if ancestor_group_ids:
        grantee |= Q(grantee_tenant_group_id__in=ancestor_group_ids)
    return (
        TenantResourceGrant.objects.filter(resource_type=content_type)
        .filter(grantee)
        .values_list("resource_id", flat=True)
    )


_ACCESS_ORDER = {"view": 0, "use": 1}

REASON_SAME_TENANT = "same-tenant"
REASON_DIRECT_GRANT = "direct-grant"
REASON_GROUP_GRANT = "group-grant"
DENIED_NO_ACTIVE_TENANT = "no-active-tenant"
DENIED_OWNER_UNRESOLVABLE = "owner-unresolvable"
DENIED_NO_GRANT = "no-grant"
DENIED_INSUFFICIENT_LEVEL = "insufficient-access-level"
DENIED_RBAC = "rbac-denied"
DENIED_UNSUPPORTED_RESOURCE = "unsupported-resource-type"
DENIED_INVALID_ACCESS_LEVEL = "invalid-access-level"


@dataclass(frozen=True)
class ResourceAccessDecision:
    allowed: bool
    reason: str
    owner_tenant_id: Optional[int] = None
    grant: Optional[object] = None
    system_authorization: Optional[SystemAuthorizationContext] = None


@dataclass(frozen=True)
class _BatchResolverEvidence:
    active_tenant_id: int
    owner_by_stock_id: dict
    direct_grants_by_stock_id: dict
    group_grants_by_stock_id: dict
    rbac_allowed: bool


def resolve_stock_access(
    user: object | None,
    stock: object,
    access_level: str,
    perm: str,
    active_tenant: object | None = None,
    system_authorization: SystemAuthorizationContext | None = None,
    system_operation: str | None = None,
    lock_grant: bool = False,
) -> ResourceAccessDecision:
    """Return the canonical stock-pool authorization decision.

    Batch evidence is deliberately absent from this public signature. Only the
    private batch projector below can supply preloaded database evidence.
    """
    return _resolve_stock_access(
        user,
        stock,
        access_level,
        perm,
        active_tenant=active_tenant,
        system_authorization=system_authorization,
        system_operation=system_operation,
        lock_grant=lock_grant,
    )


def _resolve_stock_access(
    user,
    stock,
    access_level,
    perm,
    active_tenant=None,
    system_authorization=None,
    system_operation=None,
    lock_grant=False,
    _evidence=None,
):
    """Resolver implementation, optionally using internal batch evidence."""
    # inline import: app-registry: this module is imported while Django loads apps.
    from organization.models import Tenant

    if active_tenant is None:
        active_tenant = core_context.get_current_tenant()
    active_tenant_id = getattr(active_tenant, "pk", None)
    active_tenant_is_live = (_evidence is not None and _evidence.active_tenant_id == active_tenant_id) or (
        active_tenant_id is not None
        and Tenant._base_manager.filter(
            pk=active_tenant_id,
            deleted_at__isnull=True,
        ).exists()
    )
    if not active_tenant_is_live:
        return ResourceAccessDecision(False, DENIED_NO_ACTIVE_TENANT)
    resource_type = _approved_resource_type(stock)
    if resource_type is None:
        return ResourceAccessDecision(False, DENIED_UNSUPPORTED_RESOURCE)
    owner_tenant_id = _live_owner_tenant_id(stock, _evidence)
    if owner_tenant_id is None:
        return ResourceAccessDecision(False, DENIED_OWNER_UNRESOLVABLE)
    if access_level not in _ACCESS_ORDER:
        return ResourceAccessDecision(False, DENIED_INVALID_ACCESS_LEVEL, owner_tenant_id)
    if owner_tenant_id == active_tenant.pk:
        return _same_tenant_decision(
            user,
            active_tenant,
            perm,
            owner_tenant_id,
            system_authorization,
            system_operation,
            _evidence,
        )
    return _cross_tenant_decision(
        user,
        stock,
        access_level,
        perm,
        active_tenant,
        owner_tenant_id,
        resource_type,
        system_authorization,
        system_operation,
        _evidence,
        lock_grant,
    )


def authorize_tenant_operation(
    user: object | None,
    active_tenant: object | None,
    perm: str,
    *,
    system_authorization: SystemAuthorizationContext | None = None,
    system_operation: str | None = None,
) -> bool:
    """Authorize one actor or issued-system operation in a live tenant."""
    # inline import: app-registry: this module is imported while Django loads apps.
    from organization.models import Tenant

    active_tenant_id = getattr(active_tenant, "pk", None)
    if (
        active_tenant_id is None
        or not Tenant._base_manager.filter(
            pk=active_tenant_id,
            deleted_at__isnull=True,
        ).exists()
    ):
        return False
    return _actor_or_system_has_permission(
        user,
        active_tenant,
        perm,
        system_authorization,
        system_operation,
    )


def resolved_shared_stock_ids(
    stock_model: type[object],
    active_tenant: object | None,
    user: object | None,
    access_level: str,
    perm: str,
    system_authorization: SystemAuthorizationContext | None = None,
    system_operation: str | None = None,
) -> list[int]:
    """Batch DB evidence, then project every candidate through the resolver."""
    # inline imports: app-registry: this module is imported while Django loads apps.
    from django.contrib.contenttypes.models import ContentType

    # inline import: app-registry: this module is imported while Django loads apps.
    from organization.models import Tenant, TenantResourceGrant

    active_tenant_id = getattr(active_tenant, "pk", None)
    active_row = (
        Tenant._base_manager.filter(
            pk=active_tenant_id,
            deleted_at__isnull=True,
        )
        .values("group_id")
        .first()
    )
    if active_row is None or access_level not in _ACCESS_ORDER:
        return []
    if stock_model._meta.label_lower not in TenantResourceGrant.APPROVED_RESOURCE_MODELS:
        return []
    rbac_allowed = _actor_or_system_has_permission(
        user,
        active_tenant,
        perm,
        system_authorization,
        system_operation,
    )
    if not rbac_allowed:
        return []
    ancestor_group_ids = get_ancestor_tenant_group_ids(
        active_row["group_id"],
        live_only=True,
    )
    grantee = Q(
        grantee_tenant_id=active_tenant_id,
        grantee_tenant__deleted_at__isnull=True,
    )
    if ancestor_group_ids:
        grantee |= Q(
            grantee_tenant_group_id__in=ancestor_group_ids,
            grantee_tenant_group__deleted_at__isnull=True,
        )
    resource_type = ContentType.objects.get_for_model(stock_model)
    grants = list(
        TenantResourceGrant.objects.filter(
            resource_type=resource_type,
            tenant__deleted_at__isnull=True,
        )
        .filter(grantee)
        .order_by("created_at", "pk")
    )
    direct_grants = {}
    group_grants = {}
    for grant in grants:
        target = direct_grants if grant.grantee_tenant_id is not None else group_grants
        target.setdefault(grant.resource_id, []).append(grant)
    candidate_ids = set(direct_grants) | set(group_grants)
    candidates = list(
        stock_model._base_manager.filter(
            pk__in=candidate_ids,
            location__deleted_at__isnull=True,
            location__tenant__deleted_at__isnull=True,
        ).select_related("location", "location__tenant")
    )
    evidence = _BatchResolverEvidence(
        active_tenant_id=active_tenant_id,
        owner_by_stock_id={stock.pk: stock.location.tenant_id for stock in candidates},
        direct_grants_by_stock_id=direct_grants,
        group_grants_by_stock_id=group_grants,
        rbac_allowed=rbac_allowed,
    )
    return [
        stock.pk
        for stock in candidates
        if _resolve_stock_access(
            user,
            stock,
            access_level,
            perm,
            active_tenant=active_tenant,
            system_authorization=system_authorization,
            system_operation=system_operation,
            _evidence=evidence,
        ).allowed
    ]


def shared_stock_read_allowed(
    obj: object,
    active_tenant: object | None,
    user: object | None,
    perm: str | None = None,
) -> bool:
    """Framework-facing facade for canonical shared-stock read decisions."""
    # inline import: app-registry: this module is imported while Django loads apps.
    from organization.models import TenantResourceGrant

    if obj._meta.label_lower not in TenantResourceGrant.APPROVED_RESOURCE_MODELS:
        return False
    required_perm = perm or f"{obj._meta.app_label}.view_{obj._meta.model_name}"
    return resolve_stock_access(
        user,
        obj,
        TenantResourceGrant.ACCESS_VIEW,
        required_perm,
        active_tenant=active_tenant,
    ).allowed


def _actor_or_system_has_permission(
    user,
    active_tenant,
    perm,
    system_authorization,
    system_operation,
    evidence=None,
):
    if user is not None:
        if evidence is not None:
            return evidence.rbac_allowed
        return user.has_perm(perm, obj=active_tenant)
    current_tenant = core_context.get_current_tenant()
    return (
        isinstance(system_authorization, SystemAuthorizationContext)
        and system_operation is not None
        and getattr(current_tenant, "pk", None) == active_tenant.pk
        and system_authorization.is_valid_for(
            tenant_id=active_tenant.pk,
            permission=perm,
            operation=system_operation,
            request_id=get_current_request_id(),
        )
    )


def _same_tenant_decision(
    user,
    active_tenant,
    perm,
    owner_tenant_id,
    system_authorization,
    system_operation,
    evidence,
):
    if not _actor_or_system_has_permission(
        user,
        active_tenant,
        perm,
        system_authorization,
        system_operation,
        evidence,
    ):
        return ResourceAccessDecision(False, DENIED_RBAC, owner_tenant_id)
    return ResourceAccessDecision(
        True,
        REASON_SAME_TENANT,
        owner_tenant_id,
        system_authorization=system_authorization if user is None else None,
    )


def _cross_tenant_decision(
    user,
    stock,
    access_level,
    perm,
    active_tenant,
    owner_tenant_id,
    resource_type,
    system_authorization,
    system_operation,
    evidence,
    lock_grant,
):
    grant, reason = _find_covering_grant(
        owner_tenant_id,
        active_tenant,
        stock,
        resource_type,
        evidence,
        lock_grant,
    )
    if grant is None:
        return ResourceAccessDecision(False, DENIED_NO_GRANT, owner_tenant_id)
    if grant.access_level not in _ACCESS_ORDER:
        return ResourceAccessDecision(False, DENIED_INVALID_ACCESS_LEVEL, owner_tenant_id, grant)
    if _ACCESS_ORDER[grant.access_level] < _ACCESS_ORDER[access_level]:
        return ResourceAccessDecision(False, DENIED_INSUFFICIENT_LEVEL, owner_tenant_id, grant)
    if not _actor_or_system_has_permission(
        user,
        active_tenant,
        perm,
        system_authorization,
        system_operation,
        evidence,
    ):
        return ResourceAccessDecision(False, DENIED_RBAC, owner_tenant_id, grant)
    return ResourceAccessDecision(
        True,
        reason,
        owner_tenant_id,
        grant,
        system_authorization if user is None else None,
    )


def _approved_resource_type(stock):
    # inline imports: app-registry: this module is imported while Django loads apps.
    from django.contrib.contenttypes.models import ContentType

    # inline import: app-registry: this module is imported while Django loads apps.
    from organization.models import TenantResourceGrant

    try:
        resource_type = ContentType.objects.get_for_model(type(stock))
    except (AttributeError, TypeError):
        return None
    label = f"{resource_type.app_label}.{resource_type.model}"
    if label not in TenantResourceGrant.APPROVED_RESOURCE_MODELS:
        return None
    return resource_type


def _live_owner_tenant_id(stock, evidence=None):
    # inline import: app-registry: this module is imported while Django loads apps.
    from organization.models import Location, Tenant

    stock_id = getattr(stock, "pk", None)
    if evidence is not None:
        return evidence.owner_by_stock_id.get(stock_id)
    if stock_id is None:
        return None
    persisted = type(stock)._base_manager.filter(pk=stock_id).values_list("location_id", "location__tenant_id").first()
    if persisted is None:
        return None
    location_id, owner_tenant_id = persisted
    if location_id is None or owner_tenant_id is None:
        return None
    if not Location._base_manager.filter(
        pk=location_id,
        tenant_id=owner_tenant_id,
        deleted_at__isnull=True,
    ).exists():
        return None
    if not Tenant._base_manager.filter(
        pk=owner_tenant_id,
        deleted_at__isnull=True,
    ).exists():
        return None
    return owner_tenant_id


def _find_covering_grant(owner_tenant_id, active_tenant, stock, resource_type, evidence=None, lock_grant=False):
    # inline import: app-registry: this module is imported while Django loads apps.
    from organization.models import TenantResourceGrant

    if evidence is not None:
        direct = next(
            (
                grant
                for grant in evidence.direct_grants_by_stock_id.get(stock.pk, ())
                if grant.tenant_id == owner_tenant_id
            ),
            None,
        )
        if direct is not None:
            return direct, REASON_DIRECT_GRANT
        group = next(
            (
                grant
                for grant in evidence.group_grants_by_stock_id.get(stock.pk, ())
                if grant.tenant_id == owner_tenant_id
            ),
            None,
        )
        return (group, REASON_GROUP_GRANT) if group is not None else (None, None)

    base = TenantResourceGrant.objects.filter(
        tenant_id=owner_tenant_id,
        tenant__deleted_at__isnull=True,
        resource_type=resource_type,
        resource_id=stock.pk,
    )
    if lock_grant:
        base = base.select_for_update()
    grant = base.filter(
        grantee_tenant=active_tenant,
        grantee_tenant__deleted_at__isnull=True,
    ).first()
    if grant is not None:
        return grant, REASON_DIRECT_GRANT
    # Reload topology from the database: caller-held Tenant instances may be stale.
    # inline import: app-registry: this module is imported while Django loads apps.
    from organization.models import Tenant

    active_group_id = (
        Tenant._base_manager.filter(
            pk=active_tenant.pk,
            deleted_at__isnull=True,
        )
        .values_list("group_id", flat=True)
        .first()
    )
    ancestor_group_ids = get_ancestor_tenant_group_ids(active_group_id, live_only=True)
    if ancestor_group_ids:
        grant = base.filter(grantee_tenant_group_id__in=ancestor_group_ids).order_by("created_at").first()
        if grant is not None:
            return grant, REASON_GROUP_GRANT
    return None, None


def accessible_tenant_ids_with_expiry(user: object | None) -> tuple[frozenset[int], datetime.datetime | None]:
    """``(frozenset(tenant_ids), valid_until)`` — see ``accessible_tenant_ids``
    for the memoization contract this adds to. ``valid_until`` is the earliest
    time the memo can go stale purely from the clock: a ``RoleGrant.valid_until``
    lapsing fires no save/signal, so a memo keyed only on the write-driven cache
    generation would keep serving an expired grant's tenant forever. ``None``
    means nothing cached here carries a clock-based expiry.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return frozenset(), None
    cache_key = "_accessible_tenant_ids"
    expiry_key = "_accessible_tenant_ids_valid_until"
    # Request-local memoization keyed to the user instance. filter_by_tenant()
    # calls this for EVERY tenant-scoped model rendered on a page; under a
    # tenant-group scope that recomputed the full grant walk dozens of times per
    # request, turning a query-heavy page into a ~20s wait. The
    # shared authorization-cache generation — bumped by every Membership /
    # RoleGrant / RoleGrantScope / GroupMembership / UserGroup / Role write and
    # by Tenant / TenantGroup topology changes (organization.signals) — is
    # consulted first, so the memo can never serve a write-invalidated
    # accessible set; the valid_until check below additionally covers the
    # signal-less case of a grant expiring purely by the clock. A cache-backend
    # outage makes synchronize_authorization_cache() fail open to a fresh local
    # version on every call, which forces a recompute below rather than ever
    # serving a stale set while the shared cache is unreachable.
    can_cache = hasattr(user, "__dict__")
    if can_cache:
        synchronize_authorization_cache(user)
        cached = user.__dict__.get(cache_key)
        if cached is not None:
            cached_valid_until = user.__dict__.get(expiry_key)
            if cached_valid_until is None or cached_valid_until > timezone.now():
                return cached, cached_valid_until
    result, valid_until = _rbac.resolve_accessible_tenant_ids_with_expiry(user)
    result = frozenset(result)
    if can_cache:
        user.__dict__[cache_key] = result
        user.__dict__[expiry_key] = valid_until
    return result, valid_until


def accessible_tenant_ids(user: object | None) -> set[int]:
    # Copy out so a caller mutating the set can't corrupt the memo.
    return set(accessible_tenant_ids_with_expiry(user)[0])


def managed_accessible_tenant_ids(user: object | None) -> set[int]:
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    tenant_ids = set()
    for grant in _rbac.applicable_grants(user):
        tenant_ids.update(grant.scoped_tenant_ids())
    return tenant_ids


def tenant_access_report(tenant: object, external_only: bool = False) -> list[dict[str, object]]:
    """Return users who can access ``tenant`` with native grant provenance."""
    # inline import: app-registry: organization models are not loadable while
    # Django is still loading apps, and this module is imported during app setup.
    from organization.models import Membership, RoleGrant

    user_data = {}

    def entry_for(user):
        if user.pk not in user_data:
            user_data[user.pk] = {
                "user": user,
                "sources": set(),
                "groups": set(),
                "permissions": set(),
            }
        return user_data[user.pk]

    if not external_only:
        memberships = Membership.objects.filter(
            tenant=tenant,
            is_active=True,
        ).select_related("user")
        for membership in memberships:
            entry_for(membership.user)["sources"].add("membership")

    grants = (
        RoleGrant.objects.filter(role__deleted_at__isnull=True)
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=timezone.now()))
        .select_related(
            "membership__user",
            "membership__tenant",
            "user_group__tenant",
            "role__tenant",
        )
        .prefetch_related(
            "scopes",
            "scopes__tenant",
            "scopes__tenant_group",
            "user_group__group_memberships__membership__user",
        )
    )
    for grant in grants:
        if not grant.covers_tenant(tenant):
            continue
        if grant.membership_id:
            if not grant.membership.is_active:
                continue
            entry = entry_for(grant.membership.user)
            source = "membership" if grant.membership.tenant_id == tenant.pk else "managed"
            entry["sources"].add(source)
            entry["permissions"].update(grant.role.permissions or [])
            continue

        group = grant.user_group
        if not group.is_active or group.deleted_at is not None:
            continue
        for group_membership in group.group_memberships.all():
            membership = group_membership.membership
            if not membership.is_active or membership.tenant_id != group.tenant_id:
                continue
            entry = entry_for(membership.user)
            entry["sources"].add("group")
            if group.tenant_id != tenant.pk:
                entry["sources"].add("managed")
            entry["groups"].add(group.name)
            entry["permissions"].update(grant.role.permissions or [])

    if external_only:
        local_user_ids = set(Membership.objects.filter(tenant=tenant).values_list("user_id", flat=True))
        user_data = {pk: data for pk, data in user_data.items() if pk not in local_user_ids}

    report = []
    for data in user_data.values():
        user = data["user"]
        report.append(
            {
                "user": user,
                "sources": sorted(data["sources"]),
                "groups": sorted(data["groups"]),
                "permissions": sorted(data["permissions"]),
                "inactive": not (user.is_active and getattr(user, "can_login", True)),
            }
        )
    report.sort(key=lambda row: (row["user"].username or "").lower())
    return report

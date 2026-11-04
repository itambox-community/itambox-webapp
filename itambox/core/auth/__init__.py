import logging

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.utils import timezone

from core.authorization_cache import synchronize_authorization_cache

# Read the request context from the leaf module rather than from
# ``core.managers``: the managers import this package for
# its authorization cache, so importing them back here closed an import loop.
from core.context import (
    get_current_all_accessible,
    get_current_membership,
    get_current_scope_conflict,
    get_current_tenant,
    get_current_tenant_group,
)
from core.tenant_scope import (
    accessible_tenant_ids_with_expiry,
    build_accessible_tenant_permissions_map,
    get_descendant_tenant_group_ids,
    managed_accessible_tenant_ids,
    resolve_effective_permissions_with_expiry,
)

logger = logging.getLogger(__name__)
User = get_user_model()


class PasswordLoginOnlyBackend(ModelBackend):
    """Delegates ``authenticate(username/password)`` to ModelBackend but rejects all
    permission checks so the only path that grants permissions is the unified
    :class:`MembershipBackend` below.
    """

    def user_can_authenticate(self, user):
        # Standard ModelBackend hook (also checks ``is_active``). A user with
        # ``can_login=False`` may never perform interactive password login — independent of
        # ``is_active`` (account status) and of API-token access. ``getattr`` keeps this safe
        # if the user model lacks the field (e.g. during a partial migration).
        return super().user_can_authenticate(user) and getattr(user, "can_login", True)

    def has_perm(self, user_obj, perm, obj=None):
        return False

    def has_module_perms(self, user_obj, app_label):
        return False

    def get_all_permissions(self, user_obj, obj=None):
        return set()

    def get_user_permissions(self, user_obj, obj=None):
        return set()

    def get_group_permissions(self, user_obj, obj=None):
        return set()


class MembershipBackend:
    """Unified RBAC backend — one container type (Tenant), one vocabulary.

    ``user.has_perm(perm, obj=...)`` is the only authorization currency. A user's
    permissions inside a tenant ``T`` are the additive union of:

      1. direct ``RoleGrant`` rows on an active Membership;
      2. group ``RoleGrant`` rows inherited through membership-backed groups;
      3. additive ``RoleGrantScope`` children that cover ``T``.

    There is no second resolution path: administering a provider tenant is simply
    holding permissions inside THAT tenant (rule 1/2 there), and nothing is
    stripped in the managed projection — role content alone decides what a grant
    conveys, and the escalation guard decides who may create it.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        return None

    # ------------------------------------------------------------------ tenant perms
    def _effective_perms_for_tenant(self, user_obj, tenant):
        """Frozen union of permissions the user holds inside ``tenant``.

        Cached on the user as ``_perms_tenant_<pk>`` so the dozens of ``has_perm`` checks
        in a single request cost at most three queries.
        """
        synchronize_authorization_cache(user_obj)
        cache_key = f"_perms_tenant_{tenant.pk}"
        if hasattr(user_obj, cache_key):
            permissions, valid_until = getattr(user_obj, cache_key)
            if valid_until is None or valid_until > timezone.now():
                return permissions

        permissions, valid_until = resolve_effective_permissions_with_expiry(
            user_obj,
            tenant,
        )
        setattr(user_obj, cache_key, (permissions, valid_until))
        return permissions

    # ------------------------------------------------------------------ context resolution
    def _object_tenant(self, user_obj, obj):
        """The tenant carried by ``obj`` (a Tenant instance IS its own context), or
        ``None`` for a tenant-less (global/shared) object."""
        Tenant = apps.get_model("organization", "Tenant")

        obj_tenant = getattr(obj, "tenant", None)
        if obj_tenant is None and isinstance(obj, Tenant):
            obj_tenant = obj
        return obj_tenant

    def _group_scope_tenants(self, user_obj):
        """Tenants the ambient permission gate must aggregate over, or ``None``
        when a single active tenant (or no scope) applies.

        Under a tenant-group scope this is the canonical accessible set
        (memberships, UserGroup grants, managed reach) intersected with the
        scoped group's subtree; under the "All accessible tenants" scope it is
        the full accessible set. Either way the ambient gate agrees with what the
        scoped querysets will show. Cached on the user for the request's many
        ``has_perm`` calls.
        """
        synchronize_authorization_cache(user_obj)
        if get_current_tenant() is not None:
            return None
        if get_current_all_accessible():
            # "All accessible tenants" scope: the gate passes when the permission
            # is held in ANY canonically accessible tenant, and fails closed when
            # it is held in none — mirroring the tenant-group union below.
            cache_key = "_all_accessible_scope_tenants"
            if hasattr(user_obj, cache_key):
                tenants, valid_until = getattr(user_obj, cache_key)
                if valid_until is None or valid_until > timezone.now():
                    return tenants
            Tenant = apps.get_model("organization", "Tenant")
            ids, valid_until = accessible_tenant_ids_with_expiry(user_obj)
            tenants = list(
                Tenant._base_manager.filter(
                    pk__in=ids,
                    deleted_at__isnull=True,
                )
            )
            setattr(user_obj, cache_key, (tenants, valid_until))
            return tenants
        group = get_current_tenant_group()
        if group is None:
            return None
        cache_key = f"_group_scope_tenants_{group.pk}"
        if hasattr(user_obj, cache_key):
            tenants, valid_until = getattr(user_obj, cache_key)
            if valid_until is None or valid_until > timezone.now():
                return tenants
        Tenant = apps.get_model("organization", "Tenant")
        # live_only: prune soft-deleted subgroups exactly like filter_by_tenant's
        # walk, so the gate never counts a tenant the scoped querysets will hide.
        ids, valid_until = accessible_tenant_ids_with_expiry(user_obj)
        tenants = list(
            Tenant._base_manager.filter(
                pk__in=ids,
                group_id__in=get_descendant_tenant_group_ids(group.pk, live_only=True),
                deleted_at__isnull=True,
            )
        )
        setattr(user_obj, cache_key, (tenants, valid_until))
        return tenants

    def _ambient_tenant(self, user_obj):
        """Single-tenant ambient context: bound membership → current tenant →
        first active membership's tenant → first scoped managed tenant.

        Read-only: the result is a local; a permission check never binds scope.
        Fallbacks are ordered deterministically (tenant name, pk)."""
        Membership = apps.get_model("organization", "Membership")
        Tenant = apps.get_model("organization", "Tenant")

        membership = get_current_membership()
        if membership and membership.user_id == user_obj.pk and membership.tenant_id:
            return membership.tenant
        current_tenant = get_current_tenant()
        if current_tenant is not None:
            return current_tenant
        first = (
            Membership.objects.filter(
                user=user_obj,
                is_active=True,
            )
            .select_related("tenant")
            .order_by("tenant__name", "pk")
            .first()
        )
        if first is not None:
            return first.tenant
        # Defensive fallback: a user whose only access is managed reach (should not
        # happen — reach rides on a membership — but fail towards a valid context).
        reachable = managed_accessible_tenant_ids(user_obj)
        if reachable:
            tenant = Tenant._base_manager.filter(pk__in=reachable).order_by("name").first()
            if tenant is not None:
                return tenant
        return None

    def _resolve_tenant(self, user_obj, obj):
        """Resolve the single-tenant context to evaluate against, or ``None``.

        Objects resolve through their ``tenant`` attribute; a tenant-less object
        falls through to the ambient chain. Group-scoped ambient checks never get
        here — ``has_perm``/``has_module_perms`` branch to the group union first.
        """
        if obj is not None:
            obj_tenant = self._object_tenant(user_obj, obj)
            if obj_tenant is not None:
                return obj_tenant
        return self._ambient_tenant(user_obj)

    # ------------------------------------------------------------------ public API
    def _all_accessible_permissions(self, user_obj):
        """Union of all permissions across all accessible tenants.

        Memoized on the user under ``_all_accessible_perms`` so ambient
        ``has_perm`` / ``has_module_perms`` checks under the all-accessible
        scope become single set lookups instead of iterating every tenant
        on every call (fix #2 for issue #56).

        Prebuilds ``build_accessible_tenant_permissions_map`` once before the
        per-tenant loop below (phase 3 correction for issue #56): that map is
        a single-pass precompute of the same ``covers_tenant()`` union each
        ``_effective_perms_for_tenant`` call below would otherwise re-derive
        per tenant, but nothing called it in production, so it stayed dormant
        and the per-tenant loop kept paying the full per-grant walk N times.
        """
        synchronize_authorization_cache(user_obj)
        cached = user_obj.__dict__.get("_all_accessible_perms")
        if cached is not None:
            return cached
        group_tenants = self._group_scope_tenants(user_obj)
        if group_tenants is None:
            return frozenset()
        build_accessible_tenant_permissions_map(user_obj)
        all_perms = set()
        for tenant in group_tenants:
            all_perms.update(self._effective_perms_for_tenant(user_obj, tenant))
        result = frozenset(all_perms)
        user_obj.__dict__["_all_accessible_perms"] = result
        return result

    @staticmethod
    def _aggregate_scope_allows_ambient_permission(perm):
        """Allow read and tenant-selecting create gates in aggregate scopes.

        All-accessible has no single tenant anchor. Objectless change/delete or
        transaction permissions would therefore authorize an unanchored mutation
        against an arbitrary tenant. ``add_*`` is the deliberate exception for
        create forms: the form must select a concrete tenant and the generic edit
        view re-checks that tenant-bound permission in ``form_valid``.
        """
        codename = perm.rsplit(".", 1)[-1]
        return codename.startswith(("view_", "add_"))

    @staticmethod
    def _aggregate_scope_allows_module_permission(perm):
        """Keep aggregate navigation read-oriented even when add is available."""
        codename = perm.rsplit(".", 1)[-1]
        return codename.startswith("view_")

    def has_perm(self, user_obj, perm, obj=None):
        if not user_obj.is_active:
            return False
        if user_obj.is_superuser:
            return True
        if get_current_scope_conflict(user_obj):
            return False
        tenant = self._object_tenant(user_obj, obj) if obj is not None else None
        if tenant is None:
            if get_current_all_accessible() and not self._aggregate_scope_allows_ambient_permission(perm):
                return False
            # Ambient check (list/add gates, nav). Under an active tenant-group
            # scope the page aggregates every accessible tenant in the subtree,
            # so the gate passes when the permission is held in ANY of them and
            # fails closed when it is held in none. Anchoring at the user's
            # first membership instead would 403 a managed-only user whose home
            # (provider) membership carries no own-reach roles — and the
            # first-membership fallback would stomp the group context mid-request.
            group_tenants = self._group_scope_tenants(user_obj)
            if group_tenants is not None:
                if get_current_all_accessible():
                    return perm in self._all_accessible_permissions(user_obj)
                return any(perm in self._effective_perms_for_tenant(user_obj, tenant) for tenant in group_tenants)
            tenant = self._ambient_tenant(user_obj)
        if tenant is None:
            return False
        return perm in self._effective_perms_for_tenant(user_obj, tenant)

    def has_module_perms(self, user_obj, app_label):
        if not user_obj.is_active:
            return False
        if user_obj.is_superuser:
            return True
        if get_current_scope_conflict(user_obj):
            return False
        prefix = app_label + "."
        # Same group-union semantics as the ambient has_perm gate above.
        group_tenants = self._group_scope_tenants(user_obj)
        if group_tenants is not None:
            if get_current_all_accessible():
                all_perms = self._all_accessible_permissions(user_obj)
                return any(
                    p.startswith(prefix) and self._aggregate_scope_allows_module_permission(p) for p in all_perms
                )
            return any(
                p.startswith(prefix)
                and (not get_current_all_accessible() or self._aggregate_scope_allows_ambient_permission(p))
                for tenant in group_tenants
                for p in self._effective_perms_for_tenant(user_obj, tenant)
            )
        tenant = self._ambient_tenant(user_obj)
        if tenant is None:
            return False
        return any(p.startswith(prefix) for p in self._effective_perms_for_tenant(user_obj, tenant))

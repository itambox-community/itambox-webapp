from dataclasses import dataclass
from typing import Any, Optional

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist, FieldError
from django.db import models
from django.db.models import QuerySet

from core.authorization_cache import synchronize_authorization_cache

# The request-context contextvars and their accessors live in the leaf module
# ``core.context``: the middleware that populates them and
# the auth backends that read them need the same objects, and hosting them here
# forced both halves into a circular import. They are re-exported unchanged so
# the established ``from core.managers import set_current_tenant`` import sites
# keep working; ``core.context`` is the canonical home for new code.
from core.context import (  # noqa: F401 -- re-exported for existing importers
    _current_all_accessible,
    _current_membership,
    _current_tenant,
    _current_tenant_group,
    _descendant_group_ids_cache,
    get_current_all_accessible,
    get_current_membership,
    get_current_scope_conflict,
    get_current_tenant,
    get_current_tenant_group,
    get_current_user,
    set_current_all_accessible,
    set_current_membership,
    set_current_tenant,
    set_current_tenant_group,
)
from core.tenant_scope import (
    accessible_tenant_ids,
    build_accessible_tenant_permissions_map,
    get_ancestor_tenant_group_ids,
    get_descendant_tenant_group_ids,
)

_AMBIENT = object()


@dataclass(frozen=True)
class Scope:
    """One resolved tenant scope, the explicit input of ``for_scope``.

    ``kind`` is exactly one of:

    * ``TENANT``: one active tenant (``tenant``);
    * ``GROUP``: one active tenant group subtree (``group``);
    * ``ALL_ACCESSIBLE``: the canonical accessible tenant set of ``user``;
    * ``SYSTEM``: no tenant restriction (superuser, migrations, background
      work, the pre-tenant bootstrap);
    * ``DENIED``: an authenticated non-superuser without a resolved or with a
      contradictory scope; resolves to nothing.
    """

    kind: str
    user: Any = None
    tenant: Any = None
    group: Any = None

    TENANT = "tenant"
    GROUP = "group"
    ALL_ACCESSIBLE = "all_accessible"
    SYSTEM = "system"
    DENIED = "denied"

    @classmethod
    def current(cls) -> "Scope":
        """Resolve the ambient contextvars into one ``Scope`` value."""
        tenant = get_current_tenant()
        group = get_current_tenant_group()
        all_accessible = get_current_all_accessible()
        user = get_current_user()
        if tenant or group or all_accessible:
            if get_current_scope_conflict(user):
                return cls(cls.DENIED, user=user)
            if tenant:
                return cls(cls.TENANT, user=user, tenant=tenant)
            if group:
                return cls(cls.GROUP, user=user, group=group)
            return cls(cls.ALL_ACCESSIBLE, user=user)
        if user is not None and not getattr(user, "is_superuser", False):
            return cls(cls.DENIED, user=user)
        return cls(cls.SYSTEM, user=user)


@dataclass(frozen=True)
class TenantScopeDeclaration:
    """Introspectable per-model scoping declaration.

    ``strategy`` is ``SELF_TENANT`` / ``SELF_GROUP`` (the model is the tenant
    or tenant-group tree itself), ``FIELD`` (direct ``tenant`` field),
    ``LOOKUP`` (``tenant_lookup`` ORM path) or ``NONE`` (no tenant column).
    """

    strategy: str
    lookup: Optional[str] = None
    allow_global: bool = False
    deny_global: bool = False
    has_tenant_group: bool = False
    has_filter_tenants: bool = False

    SELF_TENANT = "self_tenant"
    SELF_GROUP = "self_group"
    FIELD = "field"
    LOOKUP = "lookup"
    NONE = "none"


def _has_field(model, name) -> bool:
    try:
        model._meta.get_field(name)
    except FieldDoesNotExist:
        return False
    return True


def tenant_scope_declaration(model) -> TenantScopeDeclaration:
    """The scoping declaration of ``model``.

    A model that IS the tenant or tenant-group tree states so with
    ``tenant_scope_self = "tenant" | "group"``; every other model is declared
    by its ``tenant`` field or its ``tenant_lookup`` path, with
    ``allow_global_tenant`` / ``deny_global_tenant`` as the global-row policy.
    """
    own = getattr(model, "tenant_scope_self", None)
    if own == "tenant":
        return TenantScopeDeclaration(TenantScopeDeclaration.SELF_TENANT)
    if own == "group":
        return TenantScopeDeclaration(TenantScopeDeclaration.SELF_GROUP)
    lookup = getattr(model, "tenant_lookup", None)
    has_tenant = _has_field(model, "tenant")
    if has_tenant:
        strategy = TenantScopeDeclaration.FIELD
    elif lookup:
        strategy = TenantScopeDeclaration.LOOKUP
    else:
        strategy = TenantScopeDeclaration.NONE
    return TenantScopeDeclaration(
        strategy=strategy,
        lookup=None if has_tenant else lookup,
        allow_global=bool(getattr(model, "allow_global_tenant", False)),
        deny_global=bool(getattr(model, "deny_global_tenant", False)),
        has_tenant_group=_has_field(model, "tenant_group"),
        has_filter_tenants=has_tenant and _has_field(model, "filter_tenants"),
    )


def _descendant_group_ids(group_id):
    """Ids of ``group_id`` and its live descendants, memoized per context.

    ``_base_manager`` (unscoped): TenantGroup.objects is itself tenant-scoped,
    so using it here would recurse back into the scoping. ``exclude(seen)``: a
    parent cycle in bad data must terminate the walk, not hang every scoped
    request (mirrors the cycle-safe walk in
    ``organization.access.get_descendant_tenant_group_ids``).
    """
    if not group_id:
        return []
    cache = _descendant_group_ids_cache.get()
    if cache is None:
        cache = {}
        _descendant_group_ids_cache.set(cache)
    if group_id in cache:
        return cache[group_id]
    TenantGroup = apps.get_model("organization", "TenantGroup")
    descendant_ids = [group_id]
    seen = {group_id}
    to_check = [group_id]
    while to_check:
        children = list(
            TenantGroup._base_manager.filter(parent_id__in=to_check, deleted_at__isnull=True)
            .exclude(pk__in=seen)
            .values_list("pk", flat=True)
        )
        if not children:
            break
        seen.update(children)
        descendant_ids.extend(children)
        to_check = children
    cache[group_id] = descendant_ids
    return descendant_ids


class SoftDeleteQuerySet(models.QuerySet):
    def deleted(self) -> QuerySet:
        return self.filter(deleted_at__isnull=False)

    def active(self) -> QuerySet:
        return self.filter(deleted_at__isnull=True)


class SoftDeleteManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    def get_queryset(self) -> QuerySet:
        qs = super().get_queryset()
        try:
            return qs.filter(deleted_at__isnull=True)
        except FieldError:
            return qs


class AllObjectsManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    pass


class TenantScopingQuerySet(models.QuerySet):
    @staticmethod
    def _member_visible_group_ids(user):
        """The set of TenantGroup ids that contain a tenant ``user`` can access.

        Derived from the canonical ``accessible_tenant_ids`` (direct memberships +
        UserGroup-derived + managed reach), not direct Membership rows alone, so a
        member reaching a tenant only through a group grant or managed reach still
        sees that tenant's group. ``_base_manager`` (unscoped) keeps this off the
        tenant-scoped path and avoids recursion back into ``filter_by_tenant``.
        """
        Tenant = apps.get_model("organization", "Tenant")
        accessible = accessible_tenant_ids(user)
        group_ids = set(Tenant._base_manager.filter(pk__in=accessible).values_list("group_id", flat=True))
        group_ids.discard(None)
        return group_ids

    @staticmethod
    def _group_scope_tenant_ids(active_group, get_descendant_group_ids, Tenant, user=_AMBIENT):
        """Resolve allowed tenant ids for an active tenant-group scope.

        A member's group scope must cover EVERY tenant they can reach in the
        group — direct memberships, UserGroup-derived tenants, and
        managed-reach tenants — not just direct Membership rows, so a
        reach-only tenant does not vanish after "Show All". Intersects the
        canonical accessible set with the group subtree; superusers and
        system/anonymous contexts see the whole subtree.

        The underlying ``accessible_tenant_ids`` resolution is already
        request-memoized, but this method's own intersection SELECT still ran
        on every call — once per tenant-scoped queryset rendered under the
        scope (issue #56 phase 3). Cache the resolved id list on the user,
        keyed by group, so repeated resolution for the same user/group in one
        request costs zero further queries. ``_group_scope_tenants_`` is
        already an authorization-cache-invalidation prefix (used by the
        ambient permission gate's own per-group cache), so this key is
        covered by the same write-invalidation without any change there.
        """
        allowed_group_ids = get_descendant_group_ids(active_group.pk)
        if user is _AMBIENT:
            user = get_current_user()
        if user and user.is_superuser:
            return list(
                Tenant._base_manager.filter(
                    group_id__in=allowed_group_ids,
                    deleted_at__isnull=True,
                ).values_list("pk", flat=True)
            )
        if user:
            can_cache = hasattr(user, "__dict__")
            cache_key = f"_group_scope_tenants_ids_{active_group.pk}"
            if can_cache:
                synchronize_authorization_cache(user)
                cached = user.__dict__.get(cache_key)
                if cached is not None:
                    return cached
            accessible = accessible_tenant_ids(user)
            result = list(
                Tenant._base_manager.filter(
                    pk__in=accessible,
                    group_id__in=allowed_group_ids,
                    deleted_at__isnull=True,
                ).values_list("pk", flat=True)
            )
            if can_cache:
                user.__dict__[cache_key] = result
            return result
        return list(
            Tenant._base_manager.filter(
                group_id__in=allowed_group_ids,
                deleted_at__isnull=True,
            ).values_list("pk", flat=True)
        )

    def _resolve_allowed_tenant_ids(self, scope, get_descendant_group_ids, Tenant):
        """Resolve the allowed tenant id set for whichever scope (single
        tenant / group / all-accessible) the ``Scope`` carries.
        """
        if scope.kind == Scope.TENANT:
            return [scope.tenant.pk]
        if scope.kind == Scope.GROUP:
            return self._group_scope_tenant_ids(scope.group, get_descendant_group_ids, Tenant, user=scope.user)
        # "All accessible tenants" scope: no single tenant or group is active,
        # but the request is NOT global. This never returns the unscoped
        # queryset, so it can never widen into the superuser/global view.
        return self._all_accessible_tenant_ids(scope.user)

    @staticmethod
    def _all_accessible_tenant_ids(user):
        """Resolve the "all accessible tenants" scope to EXACTLY the canonical
        accessible set (direct memberships, UserGroup-derived, and managed
        reach). Any principal that is not an authenticated non-superuser fails
        closed to no tenants — middleware only grants this scope to such
        members, and a superuser keeps their own global path.
        """
        if user is not None and getattr(user, "is_authenticated", False) and not getattr(user, "is_superuser", False):
            return list(accessible_tenant_ids(user))
        return []

    @staticmethod
    def _all_accessible_group_ids(user, allowed_tenant_ids, Tenant):
        """Live own/ancestor groups for the aggregate tenant set.

        Models with a ``tenant_group`` field all need the same projection. Cache
        it on the bound User instance so dozens of scoped querysets do not repeat
        one tenant-group query plus one query per ancestor depth. The shared
        authorization generation invalidates this memo on membership/grant or
        tenant/group-topology writes; a cache outage forces recomputation.
        """
        if user is None or not hasattr(user, "__dict__"):
            return frozenset()
        synchronize_authorization_cache(user)
        tenant_key = tuple(sorted(allowed_tenant_ids))
        cached = user.__dict__.get("_all_accessible_group_ids")
        if cached is not None and cached[0] == tenant_key:
            return cached[1]

        own_group_ids = set(
            Tenant._base_manager.filter(pk__in=tenant_key)
            .exclude(group_id__isnull=True)
            .values_list("group_id", flat=True)
        )
        group_ids = set()
        for own_group_id in own_group_ids:
            group_ids |= get_ancestor_tenant_group_ids(
                own_group_id,
                live_only=True,
            )
        result = frozenset(group_ids)
        user.__dict__["_all_accessible_group_ids"] = (tenant_key, result)
        return result

    def filter_by_tenant(self) -> QuerySet:
        """Scope this queryset to the ambient request/task context.

        Thin adapter over ``for_scope``: the ambient contextvars are resolved
        once into a ``Scope`` and the explicit path does the work.
        """
        return self.for_scope(Scope.current())

    @staticmethod
    def _authorized_tenant_ids(user, allowed_tenant_ids, required_permission):
        """Narrow ``allowed_tenant_ids`` to the ones where ``user`` holds
        ``required_permission``.

        A tenant-group or "all accessible tenants" scope makes every reachable
        tenant pass ``_resolve_allowed_tenant_ids``, but the caller may hold the
        operation's permission in only some of them -- the ambient ``has_perm``
        gate passes on the union, not the intersection (issue #1088). Reads
        ``build_accessible_tenant_permissions_map``, the exact per-tenant map
        ``MembershipBackend._effective_perms_for_tenant`` resolves from, so this
        narrowing agrees with the gate that already let the request through.
        Superusers and a missing user (background/system callers, which never
        pass ``required_permission``) are unaffected.
        """
        if not allowed_tenant_ids or user is None or getattr(user, "is_superuser", False):
            return allowed_tenant_ids
        permissions = (required_permission,) if isinstance(required_permission, str) else tuple(required_permission)
        permission_map = build_accessible_tenant_permissions_map(user)
        empty_perms = frozenset()
        return [
            tenant_id
            for tenant_id in allowed_tenant_ids
            if all(permission in permission_map.get(tenant_id, (empty_perms,))[0] for permission in permissions)
        ]

    def for_scope(self, scope, required_permission=None, group_scope="visible") -> QuerySet:
        """Scope this queryset to an explicit ``Scope`` value.

        The per-model behaviour comes from the model's scoping declaration
        (``tenant_scope_declaration``), never from the model's name.

        ``required_permission`` additionally narrows the allowed tenant set to
        those where ``scope.user`` holds that permission (see
        ``_authorized_tenant_ids``). Callers that already evaluated the
        operation's permission per-tenant (or that never widen past a single
        authorized tenant) can leave it ``None``.
        """
        if scope.kind == Scope.DENIED:
            # Fail closed: an authenticated, non-superuser principal with no
            # resolved (or a contradictory) tenant context sees nothing.
            return self.none()
        if scope.kind == Scope.SYSTEM:
            # Superusers, migrations, background tasks and the pre-tenant
            # bootstrap legitimately operate without a tenant.
            return self

        declaration = tenant_scope_declaration(self.model)
        if group_scope not in ("visible", "all"):
            raise ValueError("group_scope must be 'visible' or 'all'")
        Tenant = apps.get_model("organization", "Tenant")
        get_descendant_group_ids = _descendant_group_ids
        allowed_tenant_ids = self._resolve_allowed_tenant_ids(scope, get_descendant_group_ids, Tenant)
        if required_permission is not None:
            allowed_tenant_ids = self._authorized_tenant_ids(scope.user, allowed_tenant_ids, required_permission)
        if declaration.strategy == TenantScopeDeclaration.SELF_TENANT:
            return self.filter(pk__in=allowed_tenant_ids)
        if declaration.strategy == TenantScopeDeclaration.SELF_GROUP:
            return self._scope_tenant_group_rows(scope, get_descendant_group_ids)

        qs = self._scope_by_group_field(
            scope, declaration, allowed_tenant_ids, get_descendant_group_ids, Tenant, group_scope
        )
        if declaration.strategy == TenantScopeDeclaration.FIELD:
            qs = self._scope_by_tenant_field(qs, declaration, allowed_tenant_ids)
        elif declaration.strategy == TenantScopeDeclaration.LOOKUP:
            qs = self._scope_by_lookup(qs, declaration, allowed_tenant_ids)
        if group_scope == "all":
            qs = self._exclude_global_rows_for_write(qs, declaration)
        return qs

    @staticmethod
    def _exclude_global_rows_for_write(qs, declaration):
        """Strict bulk-write mode: exclude rows with no tenant or group anchor.

        A fully global row is visible to every tenant, so a per-tenant write
        grant cannot authorize changing it in a multi-tenant scope. The
        exclusion is fail-closed and applies only to ``group_scope="all"``.
        """
        if declaration.strategy == TenantScopeDeclaration.LOOKUP:
            return qs.exclude(**{f"{declaration.lookup}__isnull": True})
        if declaration.strategy == TenantScopeDeclaration.FIELD:
            if declaration.has_tenant_group:
                return qs.exclude(models.Q(tenant__isnull=True) & models.Q(tenant_group__isnull=True))
            return qs.exclude(tenant__isnull=True)
        if declaration.has_tenant_group:
            return qs.exclude(tenant_group__isnull=True)
        return qs

    def _scope_tenant_group_rows(self, scope, get_descendant_group_ids):
        """Rows of a model that IS the tenant-group tree.

        A user sees the groups that contain a tenant they can access, plus
        those groups' ancestors (the path to the root) for navigation.
        Superusers and system/anonymous contexts see all. An explicit group
        scope is a "show only this group" filter: the subtree plus ancestors,
        for everyone. The parent walk uses ``_base_manager`` so it never
        recurses through this (scoped) manager.
        """
        user = scope.user
        model = self.model

        def expand_to_ancestors(seed_ids):
            visible_ids = set()
            frontier = set(seed_ids)
            while frontier:
                visible_ids |= frontier
                parent_ids = set(
                    model._base_manager.filter(pk__in=frontier, deleted_at__isnull=True).values_list(
                        "parent_id", flat=True
                    )
                )
                parent_ids.discard(None)
                frontier = parent_ids - visible_ids
            return visible_ids

        unrestricted = user is None or getattr(user, "is_superuser", False)
        if scope.kind == Scope.GROUP:
            scope_ids = expand_to_ancestors(get_descendant_group_ids(scope.group.pk))
            if unrestricted:
                return self.filter(pk__in=scope_ids)
            # A member never sees a group none of their ACCESSIBLE tenants sit
            # in: intersect the scope with the groups of every reachable tenant
            # plus ancestors. The scoped group itself always survives.
            return self.filter(pk__in=(scope_ids & expand_to_ancestors(self._member_visible_group_ids(user))))
        if unrestricted:
            return self
        return self.filter(pk__in=expand_to_ancestors(self._member_visible_group_ids(user)))

    def _scope_by_group_field(
        self, scope, declaration, allowed_tenant_ids, get_descendant_group_ids, Tenant, group_scope="visible"
    ):
        """Narrow by the model's ``tenant_group`` field when it declares one.

        ``group_scope="visible"`` is the existential read rule: a row is visible
        from every tenant in its group's subtree, so the authorized tenants'
        groups plus their ancestors admit it. ``group_scope="all"`` is the write
        rule used by the bulk routes (which never run a per-object check): a
        group-shared row may only be written when the caller holds the
        permission in every live tenant that row affects.
        """
        if not declaration.has_tenant_group:
            return self
        allowed_group_ids = []
        if group_scope == "all":
            allowed_group_ids = self._fully_authorized_group_ids(
                scope, allowed_tenant_ids, get_descendant_group_ids, Tenant
            )
        elif scope.kind == Scope.GROUP:
            # Derive the groups from the (possibly permission-narrowed) tenant
            # set, not from every descendant of the selected group: a sibling
            # subgroup the caller holds no permission in must not contribute
            # its group-scoped rows (#1088).
            allowed_group_ids = self._group_ids_for_tenants(allowed_tenant_ids, Tenant)
        elif scope.kind == Scope.TENANT and scope.tenant.group:
            # A row scoped to group G is visible from G's subtree, so a tenant
            # sees rows scoped to its own group and that group's ancestors --
            # never a descendant group's rows.
            allowed_group_ids = list(get_ancestor_tenant_group_ids(scope.tenant.group.pk, live_only=True))
        elif scope.kind == Scope.ALL_ACCESSIBLE:
            # Derived from the canonical accessible_tenant_ids, so no extra
            # RBAC resolution; only runs for the (few) models that carry a
            # tenant_group field.
            allowed_group_ids = self._all_accessible_group_ids(scope.user, allowed_tenant_ids, Tenant)
        return self.filter(models.Q(tenant_group_id__in=allowed_group_ids) | models.Q(tenant_group__isnull=True))

    @classmethod
    def _fully_authorized_group_ids(cls, scope, allowed_tenant_ids, get_descendant_group_ids, Tenant):
        """Groups whose entire live tenant subtree the caller is authorized for.

        Used by the bulk write routes: a row scoped to group G is shared with
        every tenant in G's subtree, so holding the permission in one of them is
        not enough to update or delete it. The candidate groups depend on the
        active scope; a group with no live tenant authorizes nothing.
        """
        if scope.kind == Scope.GROUP:
            candidates = get_descendant_group_ids(scope.group.pk)
        elif scope.kind == Scope.TENANT and scope.tenant.group:
            candidates = get_ancestor_tenant_group_ids(scope.tenant.group.pk, live_only=True)
        else:
            TenantGroup = apps.get_model("organization", "TenantGroup")
            candidates = set(TenantGroup._base_manager.filter(deleted_at__isnull=True).values_list("pk", flat=True))
        authorized = set(allowed_tenant_ids)
        allowed = []
        for group_id in candidates:
            subtree = get_descendant_tenant_group_ids(group_id, live_only=True)
            tenant_ids = set(
                Tenant._base_manager.filter(group_id__in=subtree, deleted_at__isnull=True).values_list("pk", flat=True)
            )
            if tenant_ids and tenant_ids.issubset(authorized):
                allowed.append(group_id)
        return allowed

    @staticmethod
    def _group_ids_for_tenants(allowed_tenant_ids, Tenant):
        """Groups of the authorized tenants plus their ancestors.

        A group-scoped row is visible from every tenant in its group's subtree,
        so its group must be one of (or an ancestor of) the groups the
        authorized tenants live in. Deriving this from ``allowed_tenant_ids``
        keeps a permission-narrowed group scope from admitting a sibling
        subgroup (#1088).
        """
        direct_group_ids = set(
            Tenant._base_manager.filter(pk__in=allowed_tenant_ids, group_id__isnull=False).values_list(
                "group_id", flat=True
            )
        )
        allowed: set[int] = set()
        for group_id in direct_group_ids:
            allowed |= get_ancestor_tenant_group_ids(group_id, live_only=True)
        return list(allowed)

    @staticmethod
    def _scope_by_tenant_field(qs, declaration, allowed_tenant_ids):
        """Narrow by a direct ``tenant`` field (plus ``filter_tenants`` M2M)."""
        allow_global = declaration.allow_global
        if declaration.has_filter_tenants:
            cond = models.Q(tenant_id__in=allowed_tenant_ids) | models.Q(filter_tenants__id__in=allowed_tenant_ids)
            if allow_global:
                cond |= models.Q(tenant__isnull=True) & models.Q(filter_tenants__isnull=True)
            return qs.filter(cond).distinct()
        cond = models.Q(tenant_id__in=allowed_tenant_ids)
        if allow_global:
            cond |= models.Q(tenant__isnull=True)
        return qs.filter(cond)

    @staticmethod
    def _scope_by_lookup(qs, declaration, allowed_tenant_ids):
        """Narrow through ``tenant_lookup``, an ORM path to the owning tenant.

        Children of a global (tenant=None) parent stay visible by default,
        because a global catalogue parent is a normal pattern. Models that
        must NEVER be cross-tenant visible (``deny_global_tenant``) opt out.
        """
        lookup = declaration.lookup
        cond = models.Q(**{f"{lookup}_id__in": allowed_tenant_ids})
        if not declaration.deny_global:
            cond |= models.Q(**{f"{lookup}__isnull": True})
        return qs.filter(cond)


class TenantScopingManager(models.Manager.from_queryset(TenantScopingQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter_by_tenant()


class TenantScopingSoftDeleteQuerySet(SoftDeleteQuerySet, TenantScopingQuerySet):
    pass


class TenantScopingSoftDeleteManager(models.Manager.from_queryset(TenantScopingSoftDeleteQuerySet)):
    def get_queryset(self):
        qs = super().get_queryset().filter_by_tenant()
        try:
            return qs.filter(deleted_at__isnull=True)
        except FieldError:
            return qs


class ExplicitScopeSoftDeleteManager(models.Manager.from_queryset(TenantScopingSoftDeleteQuerySet)):
    """Plain default manager: soft-delete filtering only, no ambient tenant scope.

    The queryset still carries ``for_scope`` / ``filter_by_tenant``, so every
    reader scopes explicitly (``Model.objects.for_scope(Scope.current())`` or an
    explicit ``Scope``). Model internals never depend on the caller's context.
    """

    def get_queryset(self):
        qs = super().get_queryset()
        try:
            return qs.filter(deleted_at__isnull=True)
        except FieldError:
            return qs


class ExplicitScopeManager(models.Manager.from_queryset(TenantScopingQuerySet)):
    """Plain default manager for models without soft delete: no ambient tenant scope."""


class ExplicitScopeAllObjectsManager(models.Manager.from_queryset(TenantScopingSoftDeleteQuerySet)):
    """Plain including-deleted manager: no soft-delete filter, no ambient tenant scope."""


class TenantScopingAllObjectsManager(models.Manager.from_queryset(TenantScopingSoftDeleteQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter_by_tenant()

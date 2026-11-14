"""Domain-blind tenant and authorization provider contracts.

Core/framework code calls these wrappers only. The organization application
registers the concrete tenant/RBAC functions from ``AppConfig.ready`` after the
Django model registry is ready; this module never imports a domain module.
"""

import sys
from collections.abc import Callable

from django.apps import apps

from core.context import _descendant_group_ids_cache
from core.tenant_access import accessible_tenant_ids as _typed_accessible_tenant_ids

_provider_modules: dict[str, str] = {}


def register_tenant_scope_provider(**functions: Callable[..., object]) -> None:
    """Register the organization-owned tenant/RBAC implementation once apps load."""
    for name, function in functions.items():
        _provider_modules[name] = function.__module__


def _call(name: str, *args, **kwargs):
    try:
        module_name = _provider_modules[name]
    except KeyError as exc:
        raise RuntimeError(f"tenant scope provider {name!r} is not registered") from exc
    # Resolve through the owning module on every call so ``mock.patch`` on the
    # domain module keeps working exactly as before the provider indirection.
    return getattr(sys.modules[module_name], name)(*args, **kwargs)


def tenant_group_model():
    return apps.get_model("organization", "TenantGroup")


def tenant_model():
    return apps.get_model("organization", "Tenant")


def get_descendant_tenant_group_ids(group_id: int | None, live_only: bool = False) -> set[int]:
    if group_id is None:
        return set()
    cache = _descendant_group_ids_cache.get()
    if cache is None:
        cache = {}
        _descendant_group_ids_cache.set(cache)
    cache_key = (group_id, live_only)
    if cache_key in cache:
        return cache[cache_key]
    group_model = tenant_group_model()
    if live_only and not group_model._base_manager.filter(pk=group_id, deleted_at__isnull=True).exists():
        cache[cache_key] = set()
        return set()
    ids = {group_id}
    frontier = [group_id]
    while frontier:
        children = list(
            group_model._base_manager.filter(
                parent_id__in=frontier, **({"deleted_at__isnull": True} if live_only else {})
            )
            .exclude(pk__in=ids)
            .values_list("pk", flat=True)
        )
        if not children:
            break
        ids.update(children)
        frontier = children
    cache[cache_key] = ids
    return ids


def get_ancestor_tenant_group_ids(group_id: int | None, live_only: bool = False) -> set[int]:
    if group_id is None:
        return set()
    groups = tenant_group_model()._base_manager.all()
    if live_only:
        groups = groups.filter(deleted_at__isnull=True)
    parent_by_id = dict(groups.values_list("pk", "parent_id"))
    if group_id not in parent_by_id:
        return set()
    seen = set()
    node = group_id
    while node is not None and node not in seen and node in parent_by_id:
        seen.add(node)
        node = parent_by_id[node]
    return seen if node is None else set()


def accessible_tenant_ids(user):
    return _typed_accessible_tenant_ids(user)


def accessible_tenant_ids_with_expiry(user):
    return _call("accessible_tenant_ids_with_expiry", user)


def managed_accessible_tenant_ids(user):
    return _call("managed_accessible_tenant_ids", user)


def applicable_grants(user):
    return _call("applicable_grants", user)


def tenant_notification_recipients(tenant, user_ids=None):
    """Active users with a live grant covering ``tenant`` (see organization.access)."""
    return _call("tenant_notification_recipients", tenant, user_ids)


def role_grant_covers_tenant(user, tenant) -> bool:
    """Return whether a live direct or group grant covers ``tenant``.

    ``accessible_tenant_ids`` deliberately includes active memberships for
    workspace navigation.  Actor-bound task execution needs the stronger RBAC
    proof: a membership left behind after its final grant is revoked must not
    keep authorizing queued work.
    """
    return any(grant.covers_tenant(tenant) for grant in applicable_grants(user))


def resolve_effective_permissions_with_expiry(user, tenant):
    return _call("resolve_effective_permissions_with_expiry", user, tenant)


def build_accessible_tenant_permissions_map(user, grants=None):
    return _call("build_accessible_tenant_permissions_map", user, grants=grants)


def resolve_default_workspace(user):
    return _call("resolve_default_workspace", user)

"""Organization-owned template context processors."""

from collections import defaultdict
from urllib.parse import urlencode

from django.urls import NoReverseMatch, reverse
from django.utils.functional import SimpleLazyObject

from core.tenant_scope import accessible_tenant_ids
from itambox.utils import get_model_viewname
from itambox.views.generic.utils import resolve_view_model
from organization.models import Membership, Tenant

# The previous switch selection is replaced by the new target on every link.
# Pagination never carries over either: the same page number would point into a
# different result set. The notice marker is transient as well: it belongs to
# the request that performs the switch, not to the links built from its result.
_SWITCH_QUERY_KEYS = ("switch_tenant", "switch_tenant_group", "switch_all_accessible")
_DROPPED_QUERY_KEYS = frozenset(_SWITCH_QUERY_KEYS) | {"page", "scope_notice"}


def _bucket_by_group(tenants):
    """Bucket tenants alphabetically by group, with ungrouped tenants last."""
    group_map = defaultdict(list)
    for tenant in tenants:
        group_map[tenant.group].append(tenant)

    sorted_groups = sorted((group for group in group_map if group is not None), key=lambda group: group.name.lower())
    grouped = [{"group": group, "tenants": group_map[group]} for group in sorted_groups]
    if None in group_map:
        grouped.append({"group": None, "tenants": group_map[None]})
    return grouped


def _all_tenants(user):
    if not user.is_superuser:
        return []
    # unscoped: the tenant switcher lists every tenant the principal may enter, before any tenant is active
    return Tenant._base_manager.all().order_by("name")


def _grouped_tenants(user):
    if not user.is_superuser:
        return []
    # unscoped: the tenant switcher lists every tenant the principal may enter, before any tenant is active
    tenants = Tenant._base_manager.all().select_related("group").order_by("group__name", "name")
    return _bucket_by_group(tenants)


def _direct_membership_tenant_ids(user):
    """Return active direct memberships; suspended memberships cannot switch."""
    # unscoped: the tenant switcher lists every tenant the principal may enter, before any tenant is active
    return set(
        Membership._base_manager.filter(
            user=user,
            is_active=True,
        ).values_list("tenant_id", flat=True)
    )


def _own_tenants(user):
    """Return direct memberships with provider tenants pinned first."""
    if user.is_superuser:
        return []
    direct_ids = _direct_membership_tenant_ids(user)
    if not direct_ids:
        return []
    # unscoped: the tenant switcher lists every tenant the principal may enter, before any tenant is active
    return list(
        Tenant._base_manager.filter(pk__in=direct_ids, deleted_at__isnull=True).order_by("-is_provider", "name")
    )


def _grouped_managed_tenants(user):
    """Return reachable tenants without a direct membership, grouped as before."""
    if user.is_superuser:
        return []
    all_ids = accessible_tenant_ids(user)
    managed_ids = all_ids - _direct_membership_tenant_ids(user)
    if not managed_ids:
        return []
    # unscoped: the tenant switcher lists every tenant the principal may enter, before any tenant is active
    tenants = (
        Tenant._base_manager.filter(pk__in=managed_ids, deleted_at__isnull=True)
        .select_related("group")
        .order_by("group__name", "name")
    )
    return _bucket_by_group(tenants)


def _scope_switch_base_url(request):
    """Return the scope-safe destination for a switcher link.

    On an object page the switch must not stay on the same URL: the object is
    bound to one tenant, so the new scope would answer with a bare 404. Land on
    the model's list view instead, the same target the detail breadcrumb uses.
    Everywhere else the current path already renders under any scope.
    """
    match = getattr(request, "resolver_match", None)
    if match is not None and "pk" in (match.kwargs or {}):
        view_class = getattr(getattr(match, "func", None), "view_class", None)
        model = resolve_view_model(view_class) if view_class is not None else None
        if model is not None:
            try:
                return reverse(get_model_viewname(model, "list"))
            except NoReverseMatch:
                return _scope_safe_landing(request)
        # An object route without a resolvable model or list route cannot be
        # proven to render under the new scope, so it must not keep the object
        # URL: the switched scope may answer it with a bare 404.
        return _scope_safe_landing(request)
    return request.path


def _scope_safe_landing(request):
    """Return a destination that renders under any scope.

    The dashboard is scope-aware and always available, so it replaces an
    object URL that has no derivable list target.
    """
    try:
        return reverse("dashboard")
    except NoReverseMatch:
        return request.path


def _carried_query(request, *, drop_tenant):
    """Return the query string a switch link keeps, minus scope-bound parameters."""
    dropped = _DROPPED_QUERY_KEYS | {"tenant"} if drop_tenant else _DROPPED_QUERY_KEYS
    pairs = [(key, value) for key, values in request.GET.lists() for value in values if key not in dropped]
    if not pairs:
        return ""
    return "&" + urlencode(pairs)


def _scope_switch_context(request):
    """Provide the link construction for the workspace switcher.

    A switch keeps the current list context (filters, search) instead of
    replacing the whole query string, and resets pagination. The tenant filter
    is scope-bound: a selected tenant or group scope defines which tenants are
    shown, so the filter is dropped there and the middleware surfaces a visible
    notice instead of a silently empty list.
    """
    base_url = _scope_switch_base_url(request)
    if base_url != request.path:
        # The query string belongs to the object page, not to its list view.
        return {"base_url": base_url, "suffix": "", "suffix_scoped": "", "scoped_notice": ""}
    return {
        "base_url": base_url,
        "suffix": _carried_query(request, drop_tenant=False),
        "suffix_scoped": _carried_query(request, drop_tenant=True),
        "scoped_notice": "&scope_notice=filters" if request.GET.get("tenant") else "",
    }


def tenant_switcher_processor(request):
    """Provide lazy, structured tenant lists for the workspace switcher."""
    if not request.user.is_authenticated:
        return {
            "all_tenants_switcher": [],
            "grouped_tenants_switcher": [],
            "own_tenants_switcher": [],
            "grouped_managed_tenants_switcher": [],
            "scope_switch": {},
        }

    user = request.user
    return {
        "all_tenants_switcher": SimpleLazyObject(lambda: _all_tenants(user)),
        "grouped_tenants_switcher": SimpleLazyObject(lambda: _grouped_tenants(user)),
        "own_tenants_switcher": SimpleLazyObject(lambda: _own_tenants(user)),
        "grouped_managed_tenants_switcher": SimpleLazyObject(lambda: _grouped_managed_tenants(user)),
        "scope_switch": SimpleLazyObject(lambda: _scope_switch_context(request)),
    }

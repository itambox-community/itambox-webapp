import logging
from collections.abc import MutableMapping

from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext_lazy as _

from core.managers import Scope
from core.slugs import generate_unique_slug

logger = logging.getLogger(__name__)

MAX_PAGINATION_LIMIT = 200


def paginate_queryset(qs, limit=None, offset=None, max_limit=MAX_PAGINATION_LIMIT):
    """
    Paginate a queryset safely, clamping offset and limit to prevent negative slices
    or excessively large limits.
    """
    if offset is not None and offset < 0:
        raise ValueError("Offset cannot be negative.")
    if limit is not None and limit < 0:
        raise ValueError("Limit cannot be negative.")

    offset = max(offset or 0, 0)
    limit = max(min(limit or max_limit, max_limit), 0)
    return qs[offset : offset + limit]


def request_from_info(info):
    """Return the Django request behind a Strawberry ``Info`` or plain context."""
    context = info.context
    request = getattr(context, "request", None)
    if request is None and isinstance(context, dict):
        request = context.get("request")
    return request if request is not None else context


def active_tenant_from_info(info):
    return getattr(request_from_info(info), "active_tenant", None)


def read_scope_from_info(info):
    """The resolved read ``Scope`` of the request behind ``info``.

    Every GraphQL root resolver takes its read scope from here, so a tenant,
    a tenant-group and an all-accessible session narrow exactly like the REST
    API does (``for_scope``). Resolvers never filter by ``active_tenant``.
    """
    request_from_info(info)  # fail early on a context without a request
    return Scope.current()


def scoped_queryset(info, queryset, required_permission=None):
    """Narrow ``queryset`` (a manager or queryset) to the request's read scope.

    ``required_permission`` additionally narrows to the tenants where the
    caller holds that permission: under a tenant-group/all-accessible scope a
    tenant can be reachable without being authorized for this operation, and
    ``for_scope`` alone does not know the permission a resolver already
    checked via ``check_permission`` (issue #1088).
    """
    return queryset.for_scope(read_scope_from_info(info), required_permission=required_permission)


def requested_selector_from_scope(scope):
    """The explicit scope selector (as a plain mapping) equivalent to ``scope``.

    Returns ``None`` for a scope that reads nothing. Used where a root accepts
    an optional ``requestedScope`` override and defaults to the request scope.
    """
    if scope.kind == Scope.TENANT:
        return {"mode": "tenant", "tenant_id": int(scope.tenant.pk), "tenant_group_id": None}
    if scope.kind == Scope.GROUP:
        return {"mode": "tenant_group", "tenant_id": None, "tenant_group_id": int(scope.group.pk)}
    if scope.kind in (Scope.ALL_ACCESSIBLE, Scope.SYSTEM):
        return {"mode": "all_accessible", "tenant_id": None, "tenant_group_id": None}
    return None


def check_permission(info, perm, obj=None):
    user = getattr(request_from_info(info), "user", None)
    if not user or not user.is_authenticated:
        raise PermissionDenied(_("Authentication credentials were not provided."))
    if not user.has_perm(perm, obj=obj):
        raise PermissionDenied(_("Permission denied."))
    return user


def _scoped_relation_cache(info):
    """A per-request store that memoizes ``scoped_relation`` decisions.

    Strawberry resolves a relation field once per rendered row, so a list of
    N rows pointing at the same related object would otherwise repeat the
    authorization query N times. The cache key is the related object's
    model, permission and primary key; the value is only that object's
    decision. Nothing is materialized per table, so memory stays bounded by
    the number of distinct related objects actually rendered in this request
    (the same way ``request_loader_for_info`` attaches a request-scoped
    loader to ``info.context``).
    """
    context = info.context
    key = "_itambox_scoped_relation_decisions"
    if isinstance(context, MutableMapping):
        store = context.get(key)
        if store is None:
            store = {}
            context[key] = store
        return store
    store = getattr(context, key, None)
    if store is not None:
        return store
    try:
        store = {}
        setattr(context, key, store)
    except (AttributeError, TypeError):
        # A frozen/immutable context still resolves correctly, just without
        # the per-request memoization.
        return None
    return store


def scoped_relation(info, obj, permission):
    """Return ``obj`` only if it is reachable through the scope- and
    permission-narrowed queryset that governs the corresponding list.

    A relation resolver on an already-visible parent (e.g. ``LicenseNode``)
    must not re-widen access to a related row from a tenant the caller can
    merely reach but holds no permission in. A foreign-tenant relation can
    exist through an import, legacy data, or a field the model does not
    cross-check (for example ``License.supplier``), so the relation resolver
    is the remaining gate (#1088).

    Checks membership in ``type(obj).objects.for_scope(scope,
    required_permission=permission)`` -- the exact queryset
    ``scoped_queryset`` narrows the matching list to (#1100) -- rather than
    the ambient ``user.has_perm(permission, obj=obj)`` check. The permission
    backend's ambient branch evaluates the union of permissions across the
    active group scope for any tenant-less object, so a group-scoped object
    (``tenant`` null, ``tenant_group`` set) was wrongly treated like a fully
    global one and could leak across groups. ``for_scope`` already resolves
    a group-scoped row against its own group's reachable tenants, so
    checking membership there closes that gap while leaving a fully global
    row (``tenant`` and ``tenant_group`` both null) exactly as reachable as
    the list makes it. Because ``for_scope`` keeps those global catalogue
    rows reachable for every scope, the decision also requires the
    object-level permission; that check is ANDed with scope membership, so
    it can only narrow access, never widen it back across groups.

    Results are memoized per request per (model, permission, primary key),
    so repeated rows pointing at the same related object reuse the first
    decision. The check never materializes a table's primary keys: it costs
    at most one indexed ``EXISTS`` query per distinct related object, which
    keeps worker memory bounded on large tables. A related model whose
    default manager does not implement ``for_scope`` fails closed, because
    no scoped queryset exists to prove the row is list-visible.
    """
    if obj is None:
        return None
    user = getattr(request_from_info(info), "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    scope = read_scope_from_info(info)
    model = type(obj)
    manager = model._default_manager
    for_scope = getattr(manager, "for_scope", None)
    if for_scope is None:
        # Fail closed: without the tenant-scope contract there is no way to
        # prove the related row is in the same queryset the list uses, and the
        # ambient object check is exactly the widening this helper closes.
        return None
    cache = _scoped_relation_cache(info)
    cache_key = (model._meta.label, permission, obj.pk)
    if cache is not None and cache_key in cache:
        return obj if cache[cache_key] else None
    allowed = for_scope(scope, required_permission=permission).filter(pk=obj.pk).exists()
    if allowed:
        # Scope membership alone is not sufficient for fully global rows:
        # ``for_scope`` keeps tenant-less catalogue rows reachable for every
        # scope, while the list gate still requires the operation permission.
        # The object check can only narrow here, never widen, because it is
        # ANDed with the scoped membership above.
        allowed = user.has_perm(permission, obj=obj)
    if cache is not None:
        cache[cache_key] = allowed
    return obj if allowed else None


def generate_slug(instance):
    logger.debug(
        "GENERATE SLUG called for %s with current slug: %r",
        instance.__class__.__name__,
        getattr(instance, "slug", None),
    )
    if not getattr(instance, "slug", None):
        generate_unique_slug(instance, getattr(instance, "slug_source", "name"))

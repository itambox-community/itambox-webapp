import logging

from django.core.exceptions import PermissionDenied
from django.db.models import Q
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


def scoped_queryset(info, queryset):
    """Narrow ``queryset`` (a manager or queryset) to the request's read scope."""
    return queryset.for_scope(read_scope_from_info(info))


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


def get_object_or_denied(model, pk, user, tenant=None):
    try:
        qs = model.objects.all()
        if tenant and hasattr(model, "tenant"):
            if getattr(model, "allow_global_tenant", False):
                # Models that intentionally share global (tenant=None) rows across
                # tenants (e.g. the tenant-scopable Supplier catalogue) declare
                # allow_global_tenant. The scoped manager already limits
                # tenant_group visibility to the caller's groups, so narrowing to
                # the active tenant plus the shared global rows preserves that
                # semantics without widening the scope.
                qs = qs.filter(Q(tenant=tenant) | Q(tenant__isnull=True))
            else:
                qs = qs.filter(tenant=tenant)
        obj = qs.get(pk=pk)
        return obj
    except model.DoesNotExist:
        raise PermissionDenied(_("Permission denied.")) from None


def generate_slug(instance):
    logger.debug(
        "GENERATE SLUG called for %s with current slug: %r",
        instance.__class__.__name__,
        getattr(instance, "slug", None),
    )
    if not getattr(instance, "slug", None):
        generate_unique_slug(instance, getattr(instance, "slug_source", "name"))

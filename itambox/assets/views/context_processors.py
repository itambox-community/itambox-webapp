"""Template capabilities for the scanner-driven bulk actions.

The global mobile action bar and the docked bulk-action toolbar offer the same
three scan entries, so the decision whether to offer them is derived once here
instead of twice from ``perms`` in two templates that resolve permissions for
different scopes. ``_BaseBulkScanView.has_permission`` stays the authoritative
server-side gate; these flags only decide what is offered.

Why ``perms`` is not enough: its objectless check resolves against the *current*
request scope, and the aggregate ("all accessible tenants") scope deliberately
refuses objectless ``change_*`` permissions because there is no tenant anchor.
The scan flows are anchored instead by their required target-tenant field, so
the offer is valid whenever any accessible tenant grants the permission.
"""

from assets.forms.bulk_scan_forms import bulk_tenant_queryset
from assets.views.bulk_scan_views import CHECKIN_PERM, CHECKOUT_PERM, DISPOSE_PERM

#: Template variable carrying the per-action flags.
BULK_SCAN_CAPABILITIES = "bulk_scan_capabilities"


def _no_capabilities():
    return {"checkin": False, "checkout": False, "dispose": False}


def _any_tenant_grants(user, tenants, permission):
    """Whether ``user`` holds ``permission`` in at least one of ``tenants``."""
    return any(user.has_perm(permission, obj=tenant) for tenant in tenants)


def bulk_scan_capabilities(request):
    """Return ``{bulk_scan_capabilities: {checkin, checkout, dispose}}`` for the request scope.

    Mirrors ``_BaseBulkScanView.has_permission``: a concrete tenant scope asks
    that tenant; the aggregate scope asks whether *any* accessible tenant
    grants the permission, which is the same union the bulk scan pages accept
    before they show their target-tenant field.
    """
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return {BULK_SCAN_CAPABILITIES: _no_capabilities()}

    if not getattr(request, "active_all_accessible", False):
        # A concrete tenant scope resolves through that tenant. A tenant-group
        # scope has no single tenant, so this falls back to the ambient check,
        # which aggregates over the group exactly like the scan views agree to.
        active_tenant = getattr(request, "active_tenant", None)
        return {
            BULK_SCAN_CAPABILITIES: {
                "checkin": user.has_perm(CHECKIN_PERM, obj=active_tenant),
                "checkout": user.has_perm(CHECKOUT_PERM, obj=active_tenant),
                "dispose": user.has_perm(DISPOSE_PERM, obj=active_tenant),
            }
        }

    tenants = list(bulk_tenant_queryset(request))
    return {
        BULK_SCAN_CAPABILITIES: {
            "checkin": _any_tenant_grants(user, tenants, CHECKIN_PERM),
            "checkout": _any_tenant_grants(user, tenants, CHECKOUT_PERM),
            "dispose": _any_tenant_grants(user, tenants, DISPOSE_PERM),
        }
    }

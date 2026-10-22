"""Global scan-to-find endpoint: GET /scan/resolve/?code=<text>"""

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View

from assets.scanning import resolve_scanned_target
from core.tenant_scope import accessible_tenant_ids


@method_decorator(login_required, name="dispatch")
class ScanResolveView(View):
    """Resolve a scanned code to an asset URL.

    Resolution is cross-tenant for every tenant the user can access, so a
    scan succeeds regardless of which tenant is currently selected. Fail
    closed: a user with no accessible tenants (and no global superuser view)
    gets a 404 and no data.
    """

    def get(self, request, *args, **kwargs):
        # Fail closed: no accessible tenants means tenant-scoped queries open up.
        # Superusers are allowed to bypass this check as they have global view rights.
        if not request.user.is_superuser and not accessible_tenant_ids(request.user):
            return JsonResponse({"found": False}, status=404)

        # No blanket asset permission: every resolution step checks the view
        # permission of the object type it can return (assets, stock items,
        # labelled locations), so inventory-only users can scan their labels.
        code = request.GET.get("code", "").strip()
        if not code:
            return JsonResponse({"found": False}, status=400)

        target = resolve_scanned_target(code, request.user)
        if target is None:
            return JsonResponse({"found": False}, status=404)

        return JsonResponse(
            {
                "found": True,
                "url": target["url"],
                "label": target["label"],
            }
        )

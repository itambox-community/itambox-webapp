"""Scanner-driven bulk check-in & bulk disposal.

Both flows share a "scan basket" page: the user accumulates assets by camera
scan (mobile), USB/keyboard barcode scan or manual entry (desktop), then applies
one action to the whole batch. The batch is processed by a background ``Job``
so it scales and survives request timeouts.

- ``AssetScanActionResolveView`` — JSON endpoint resolving a scanned code to an
  asset plus per-mode eligibility (reused for server-side seeding).
- ``BulkCheckinScanView`` / ``BulkDisposeScanView`` — the basket pages.
- ``bulk_checkin_assets`` / ``bulk_dispose_assets`` — POST submit → enqueue Job.
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import NoReverseMatch, reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django.views import View
from django.views.generic import TemplateView
from django_q.tasks import async_task

from core.managers import Scope
from core.models import Job
from core.tenant_scope import accessible_tenant_ids
from itambox.views.generic.utils import safe_return_url

from .. import forms
from ..depreciation import compute_book_value
from ..forms.bulk_scan_forms import bulk_tenant_for_request, bulk_tenant_queryset, validate_bulk_tenant
from ..models import Asset, AssetDisposal
from ..scanning import resolve_scanned_asset

logger = logging.getLogger(__name__)

CHECKIN_PERM = "assets.change_asset"
CHECKOUT_PERM = "assets.change_asset"
DISPOSE_PERM = "assets.add_assetdisposal"


def asset_action_payload(asset, mode):
    """Build the JSON-serializable basket-row payload for an asset.

    Shared by the live resolve endpoint and the server-side seed list so the
    front-end renders seeded and scanned rows through one code path.
    """
    active = asset.active_assignment
    assigned = active.assigned_target if active else None

    eligible = True
    warning = None
    book_value = None

    if mode == "dispose":
        # Only an ACTIVE record means disposed. An archived asset without a
        # record is archived (book-value freeze), not disposed, so it stays eligible.
        # unscoped: disposal and reservation evidence includes cancelled and soft-deleted rows; asset scope narrows
        # visibility
        if AssetDisposal.all_objects.filter(asset=asset, cancelled_at__isnull=True).exists():
            eligible = False
            warning = str(_("Already disposed; this asset will be skipped."))
        bv = compute_book_value(asset)
        book_value = str(bv) if bv is not None else None
    elif mode == "checkout":
        if not asset.is_issuable:
            eligible = False
            warning = str(_("Cannot check out this asset because its status is %(status)s.")) % {
                "status": asset.get_status_display()
            }
    else:  # checkin
        if active is None and not asset.location_id:
            eligible = False
            warning = str(_("This asset is not checked out, so there is nothing to return."))

    return {
        "pk": asset.pk,
        # Immutable tenant identity travels with every row so the basket can keep
        # per-tenant state across a target-tenant change without re-resolving.
        "tenant_id": asset.tenant_id,
        "label": str(asset),
        "asset_tag": asset.asset_tag or "",
        "serial": asset.serial_number or "",
        "status": str(asset.status) if asset.status_id else "",
        "assigned_to": str(assigned) if assigned else "",
        "book_value": book_value,
        "eligible": eligible,
        "warning": warning,
    }


@method_decorator(login_required, name="dispatch")
class AssetScanActionResolveView(View):
    """Resolve a scanned code to a basket-row payload within the active tenant.

    GET /assets/scan/resolve-action/?code=<text>&mode=<checkin|dispose>
    """

    def get(self, request, *args, **kwargs):
        # Fail closed: no accessible tenants means tenant-scoped queries open up.
        # Superusers are allowed to bypass this check as they have global view rights.
        if not request.user.is_superuser and not accessible_tenant_ids(request.user):
            return JsonResponse({"found": False}, status=404)

        mode = request.GET.get("mode", "checkin")
        action_perm = DISPOSE_PERM if mode == "dispose" else CHECKIN_PERM
        target_tenant = bulk_tenant_for_request(request, request.GET.get("tenant"))
        if getattr(request, "active_all_accessible", False) and target_tenant is None:
            return JsonResponse({"found": False, "tenant_required": True}, status=400)

        if target_tenant is not None:
            has_view = request.user.has_perm("assets.view_asset", obj=target_tenant)
            has_action = request.user.has_perm(action_perm, obj=target_tenant)
        else:
            has_view = request.user.has_perm("assets.view_asset")
            has_action = request.user.has_perm(action_perm)
        if not has_view or not has_action:
            return JsonResponse({"found": False}, status=403)

        code = request.GET.get("code", "").strip()
        if not code:
            return JsonResponse({"found": False}, status=400)

        asset, ambiguous = resolve_scanned_asset(code, tenant=target_tenant)
        if asset is None:
            # An EAN matching several assets is reported distinctly so the UI
            # never treats an ambiguous scan as a plain miss (or worse, picks
            # an arbitrary asset).
            payload = {"found": False}
            if ambiguous:
                payload["ambiguous"] = True
            return JsonResponse(payload, status=404)

        payload = asset_action_payload(asset, mode)
        payload["found"] = True
        return JsonResponse(payload)


class _BaseBulkScanView(LoginRequiredMixin, PermissionRequiredMixin, TemplateView):
    template_name = "assets/bulk_scan.html"
    mode = "checkin"
    form_class = None
    submit_url_name = None
    page_title = ""
    page_pretitle = _("Bulk Actions")

    def has_permission(self):
        """Allow the page in aggregate scope, but bind mutations to its tenant field."""
        if not getattr(self.request, "active_all_accessible", False):
            return super().has_permission()

        required = self.get_permission_required()
        raw_tenant = self.request.POST.get("tenant") if self.request.method == "POST" else None
        target_tenant = bulk_tenant_for_request(self.request, raw_tenant) if raw_tenant else None
        if target_tenant is not None:
            return all(self.request.user.has_perm(permission, obj=target_tenant) for permission in required)

        # GET must be reachable so the user can make the required tenant choice.
        return any(
            all(self.request.user.has_perm(permission, obj=tenant) for permission in required)
            for tenant in bulk_tenant_queryset(self.request)
        )

    def _seed_assets(self):
        pks = [p for p in self.request.GET.getlist("pk") if p.isdigit()]
        if not pks:
            return []
        assets = Asset.objects.for_scope(Scope.current()).filter(pk__in=pks).select_related("status", "location")
        return [asset_action_payload(a, self.mode) for a in assets]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "mode": self.mode,
                "form": self.form_class(request=self.request),
                "submit_url": reverse(self.submit_url_name),
                "resolve_url": reverse("assets:asset_scan_resolve_action"),
                "seed_payloads": self._seed_assets(),
                "title": self.page_title,
                "pretitle": self.page_pretitle,
            }
        )
        return context


class BulkCheckinScanView(_BaseBulkScanView):
    permission_required = (CHECKIN_PERM,)
    mode = "checkin"
    form_class = forms.AssetBulkCheckInForm
    submit_url_name = "assets:asset_bulk_checkin"
    page_title = _("Bulk Check-in")


class BulkCheckoutScanView(_BaseBulkScanView):
    permission_required = (CHECKOUT_PERM,)
    mode = "checkout"
    form_class = forms.AssetBulkCheckOutForm
    submit_url_name = "assets:asset_bulk_checkout"
    page_title = _("Bulk Check-out")


class BulkDisposeScanView(_BaseBulkScanView):
    permission_required = (DISPOSE_PERM,)
    mode = "dispose"
    form_class = forms.AssetBulkDisposeForm
    submit_url_name = "assets:asset_bulk_dispose"
    page_title = _("Bulk Disposal")


def _job_redirect(request, job, message):
    messages.success(request, message)
    try:
        redirect_url = reverse("job_detail", kwargs={"pk": job.pk})
    except NoReverseMatch:
        redirect_url = f"/jobs/{job.pk}/"
    if request.htmx:
        response = HttpResponse(status=204)
        response["HX-Redirect"] = redirect_url
        return response
    return HttpResponseRedirect(redirect_url)


def _enqueue(job, task_path, *task_args):
    """Dispatch via on_commit unless running inline (Q_CLUSTER sync, e.g. tests)."""
    if getattr(settings, "Q_CLUSTER", {}).get("sync", False):
        async_task(task_path, *task_args)
    else:
        transaction.on_commit(lambda: async_task(task_path, *task_args))


def _submission_tenant(request, form_class, permission):
    """Resolve and authorize the tenant selected for a bulk mutation."""
    tenant, error = validate_bulk_tenant(request, form_class)
    if tenant is None:
        if getattr(request, "active_all_accessible", False):
            return None, "redirect", error or _("Select a target tenant first.")
        return None, "forbidden", None
    if not request.user.has_perm(permission, obj=tenant):
        return None, "forbidden", None
    return tenant, "ok", None


def _submission_batch_error(target_tenant, object_pks, *, issuable=False):
    """Return an error message unless every submitted PK is a live asset of ``target_tenant``.

    The batch boundary is enforced here, before a ``Job`` exists: a tampered,
    unknown, soft-deleted or foreign-tenant PK rejects the whole submission
    instead of being skipped item-by-item by the background task (which keeps
    its own checks as defence in depth).

    The message is deliberately count-only — naming or counting the *matched*
    foreign assets would leak their existence across the tenant boundary.
    """
    submitted = set(object_pks)
    # Junk can never match a PK; excluding it keeps the query valid and routes it
    # through the same rejection path. ``isdigit`` alone would admit non-ASCII
    # digits ("²") that raise on int conversion.
    candidates = {pk for pk in submitted if pk.isascii() and pk.isdigit()}
    queryset = Asset.issuable() if issuable else Asset.objects.all()
    known = {str(pk) for pk in queryset.filter(pk__in=candidates, tenant=target_tenant).values_list("pk", flat=True)}
    rejected = len(submitted - known)
    if not rejected:
        return None
    return ngettext(
        "%(count)d selected asset is not available in the target tenant. Nothing was submitted.",
        "%(count)d selected assets are not available in the target tenant. Nothing was submitted.",
        rejected,
    ) % {"count": rejected}


@login_required
def bulk_checkin_assets(request):
    if request.method != "POST":
        return HttpResponse(status=405)

    object_pks = request.POST.getlist("pk")
    fallback = reverse("assets:asset_bulk_checkin_scan")
    target_tenant, outcome, error = _submission_tenant(request, forms.AssetBulkCheckInForm, CHECKIN_PERM)
    if outcome == "forbidden":
        return HttpResponse(status=403)
    if outcome == "redirect":
        messages.error(request, error)
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), fallback))
    if not object_pks:
        messages.error(request, _("No assets selected for check-in."))
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), fallback))

    batch_error = _submission_batch_error(target_tenant, object_pks)
    if batch_error:
        messages.error(request, batch_error)
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), fallback))

    tenant_id = target_tenant.pk

    job = Job.objects.create(
        name=f"Bulk Check-in: {len(object_pks)} Assets",
        tenant=target_tenant,
        model=ContentType.objects.get_for_model(Asset),
        status=Job.STATUS_PENDING,
    )

    _enqueue(
        job,
        "assets.tasks.checkin.bulk_checkin_task",
        job.pk,
        object_pks,
        request.user.pk,
        tenant_id,
        request.POST.get("status") or None,
        request.POST.get("location") or None,
        request.POST.get("checkin_date") or None,
        request.POST.get("notes", ""),
    )

    return _job_redirect(
        request,
        job,
        _("Asynchronous check-in job '%(job)s' enqueued. Tracking progress in real-time.") % {"job": job.name},
    )


@login_required
def bulk_dispose_assets(request):
    if request.method != "POST":
        return HttpResponse(status=405)

    object_pks = request.POST.getlist("pk")
    fallback = reverse("assets:asset_bulk_dispose_scan")
    target_tenant, outcome, error = _submission_tenant(request, forms.AssetBulkDisposeForm, DISPOSE_PERM)
    if outcome == "forbidden":
        return HttpResponse(status=403)
    if outcome == "redirect":
        messages.error(request, error)
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), fallback))
    if not object_pks:
        messages.error(request, _("No assets selected for disposal."))
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), fallback))

    batch_error = _submission_batch_error(target_tenant, object_pks)
    if batch_error:
        messages.error(request, batch_error)
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), fallback))

    disposal_date = request.POST.get("disposal_date") or None
    if not disposal_date:
        messages.error(request, _("A disposal date is required."))
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), fallback))

    disposal_kwargs = {
        "disposal_method": request.POST.get("disposal_method", "destruction"),
        "disposal_date": disposal_date,
        "data_sanitization_method": request.POST.get("data_sanitization_method", "none"),
        "sanitization_certificate": request.POST.get("sanitization_certificate", ""),
        "sanitized_by": request.POST.get("sanitized_by", ""),
        "recipient": request.POST.get("recipient", ""),
        "currency": request.POST.get("currency", ""),
        "weee_compliant": bool(request.POST.get("weee_compliant")),
        "notes": request.POST.get("notes", ""),
    }
    proceeds_map = {pk: (request.POST.get(f"proceeds_{pk}") or None) for pk in object_pks}

    tenant_id = target_tenant.pk

    job = Job.objects.create(
        name=f"Bulk Disposal: {len(object_pks)} Assets",
        tenant=target_tenant,
        model=ContentType.objects.get_for_model(Asset),
        status=Job.STATUS_PENDING,
    )

    _enqueue(
        job,
        "assets.tasks.disposal.bulk_dispose_task",
        job.pk,
        object_pks,
        request.user.pk,
        tenant_id,
        disposal_kwargs,
        proceeds_map,
    )

    return _job_redirect(
        request,
        job,
        _("Asynchronous disposal job '%(job)s' enqueued. Tracking progress in real-time.") % {"job": job.name},
    )


@login_required
def bulk_checkout_assets(request):
    if request.method != "POST":
        return HttpResponse(status=405)

    object_pks = request.POST.getlist("pk")
    fallback = reverse("assets:asset_bulk_checkout_scan")
    target_tenant, outcome, error = _submission_tenant(request, forms.AssetBulkCheckOutForm, CHECKOUT_PERM)
    if outcome == "forbidden":
        return HttpResponse(status=403)
    if outcome == "redirect":
        messages.error(request, error)
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), fallback))
    if not object_pks:
        messages.error(request, _("No assets selected for check-out."))
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), fallback))

    batch_error = _submission_batch_error(target_tenant, object_pks, issuable=True)
    if batch_error:
        messages.error(request, batch_error)
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), fallback))

    targets = (
        ("assetholder", request.POST.get("asset_holder") or None),
        ("location", request.POST.get("location") or None),
        ("asset", request.POST.get("asset_target") or None),
    )
    chosen = [(t, i) for t, i in targets if i]
    if len(chosen) != 1:
        messages.error(request, _("Select exactly one check-out target: a holder, a location, or a parent asset."))
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), fallback))
    target_type_str, target_pk = chosen[0]

    tenant_id = target_tenant.pk

    job = Job.objects.create(
        name=f"Bulk Check-out: {len(object_pks)} Assets",
        tenant=target_tenant,
        model=ContentType.objects.get_for_model(Asset),
        status=Job.STATUS_PENDING,
    )

    _enqueue(
        job,
        "assets.tasks.checkout.bulk_checkout_task",
        job.pk,
        object_pks,
        target_type_str,
        target_pk,
        request.user.pk,
        request.POST.get("notes", ""),
        request.POST.get("expected_checkin") or None,
        tenant_id,
        request.POST.get("status") or None,
        request.POST.get("checkout_date") or None,
    )

    return _job_redirect(
        request,
        job,
        _("Asynchronous check-out job '%(job)s' enqueued. Tracking progress in real-time.") % {"job": job.name},
    )

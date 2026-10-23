import csv
import json
import logging

import django_tables2 as tables
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django.views.generic import View
from django_tables2.utils import A

from assets.models import StatusLabel
from assets.scanning import resolve_scanned_asset_in_queryset
from compliance.audit_services import (
    _audit_to_dict,
    _missing_asset_to_dict,
    audit_asset,
    authorized_scan_assets_queryset,
    classify_session_audits,
    close_audit_session,
    commit_audit_basket,
    expected_scan_assets_queryset,
    flag_missing_assets,
    preview_missing_assets,
    read_reconciliation_report,
    rehome_audit_session_mismatches,
)
from compliance.filters import AuditSessionFilterSet
from compliance.forms_audit import AuditBarcodeScanForm, AuditSessionForm
from compliance.forms_filter import AuditSessionFilterForm
from compliance.models import AssetAudit, AuditSession
from core.csv_utils import csv_safe
from core.forms.base import TenantScopedFormMixin
from core.managers import Scope
from core.tables import ActionsColumn, BaseTable, ToggleColumn
from itambox.views.generic import ObjectDeleteView, ObjectDetailView, ObjectEditView, ObjectListView
from itambox.views.generic.service_views import GenericTransactionView, SimplePostView

User = get_user_model()
logger = logging.getLogger(__name__)


def _read_report_or_deny(session, user):
    """Turn malformed frozen reports into a handled HTTP denial."""
    try:
        return read_reconciliation_report(session, user=user)
    except ValidationError as exc:
        raise PermissionDenied(_("The stored audit report is unavailable.")) from exc


def _classify_audit_scan(session, asset, expected_ids, observed_location):
    """Classify a scanned asset for an active audit session (idempotent, no writes).

    Returns ``(eligible, warning, classification)``.
    """
    if not observed_location:
        return False, str(_("Audit observed location must be specified.")), "unknown"
    if asset.status and asset.status.type == StatusLabel.TYPE_ARCHIVED:
        return False, str(_("Archived assets cannot be audited.")), "unknown"
    already_verified = AssetAudit.objects.filter(session=session, asset=asset).exists()
    if already_verified:
        warning = str(_("This asset has already been verified in this session."))
        if asset.id not in expected_ids:
            return False, warning, "surprise"
        if session.location_id is None or observed_location.id == session.location_id:
            return False, warning, "matched"
        return False, warning, "mismatch"
    if asset.id not in expected_ids:
        return True, None, "surprise"
    if session.location_id is None or observed_location.id == session.location_id:
        return True, None, "matched"
    return True, None, "mismatch"


class AuditSessionTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("compliance:auditsession_detail", args=[A("pk")], verbose_name=_("Name"))
    location = tables.LinkColumn(
        "organization:location_detail", args=[A("location.pk")], accessor="location.name", verbose_name=_("Location")
    )
    status = tables.Column(verbose_name=_("Status"))
    started_at = tables.DateTimeColumn(format="Y-m-d H:i")
    completed_at = tables.DateTimeColumn(format="Y-m-d H:i")
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = AuditSession
        fields = ("pk", "name", "location", "status", "started_at", "completed_at", "actions")
        default_columns = ("pk", "name", "location", "status", "started_at", "completed_at", "actions")

    def render_status(self, value, record):
        badges = {
            "planned": "bg-secondary text-secondary-fg",
            "active": "bg-primary text-primary-fg",
            "completed": "bg-success text-success-fg",
        }
        badge_class = badges.get(value, "bg-secondary text-secondary-fg")
        display = record.get_status_display() if value else _("Planned")
        from django.utils.html import format_html

        return format_html('<span class="badge {}">{}</span>', badge_class, display)

    def render_location(self, value):
        return value or _("Global (All Locations)")

    def render_completed_at(self, value):
        return value.strftime("%Y-%m-%d %H:%M") if value else _("Not completed")


class AuditSessionListView(ObjectListView):
    queryset = AuditSession.objects.select_related("location", "created_by")
    table = AuditSessionTable
    template_name = "generic/object_list.html"
    action_buttons = ("add",)
    filterset = AuditSessionFilterSet
    filterset_form = AuditSessionFilterForm


class AuditSessionCreateView(ObjectEditView):
    queryset = AuditSession.objects.all()
    model_form = AuditSessionForm
    template_name = "generic/object_edit.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        start_immediately = form.cleaned_data.get("start_immediately", True)
        form.instance.status = "active" if start_immediately else "planned"
        return super().form_valid(form)


class AuditSessionDetailView(ObjectDetailView):
    queryset = AuditSession.objects.select_related("location", "created_by").prefetch_related(
        "audits__asset", "audits__auditor"
    )
    template_name = "compliance/audits/audit_session_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        session = self.get_object()

        if session.status == "completed" and session.reconciliation_report is not None:
            # Render from the frozen stored report after applying the current actor scope.
            report = _read_report_or_deny(session, self.request.user)
            rows = report["rows"]
            ctx["total_expected"] = report["total_expected"]
            ctx["total_scanned"] = report["total_scanned"]
            ctx["matching"] = [r for r in rows if r["category"] == "matching"]
            ctx["mismatches"] = [r for r in rows if r["category"] == "mismatched"]
            ctx["surprise_finds"] = [r for r in rows if r["category"] == "surprise"]
            ctx["missing_assets"] = [r for r in rows if r["category"] == "missing"]
            ctx["report_is_stored"] = True
        else:
            classified = classify_session_audits(session, user=self.request.user)
            expected_loc_name = session.location.name if session.location else "Global"
            ctx["total_expected"] = (
                len(classified["matching"]) + len(classified["mismatched"]) + classified["missing"].count()
            )
            ctx["total_scanned"] = (
                len(classified["matching"]) + len(classified["mismatched"]) + len(classified["surprise"])
            )
            ctx["matching"] = [_audit_to_dict(a, "matching") for a in classified["matching"]]
            ctx["mismatches"] = [_audit_to_dict(a, "mismatched", expected_loc_name) for a in classified["mismatched"]]
            ctx["surprise_finds"] = [_audit_to_dict(a, "surprise") for a in classified["surprise"]]
            ctx["missing_assets"] = [_missing_asset_to_dict(a, session.location) for a in classified["missing"]]
            ctx["report_is_stored"] = False

        ctx["scan_form"] = AuditBarcodeScanForm()
        return ctx


class AssetAuditScanView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """HTMX AJAX endpoint called on barcode scans within an active campaign."""

    permission_required = "compliance.add_assetaudit"

    def handle_no_permission(self):
        if self.request.user.is_authenticated and getattr(self.request, "htmx", False):
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps(
                {
                    "showMessage": {
                        "message": str(_("You do not have permission to record audit scans.")),
                        "level": "danger",
                    }
                }
            )
            return response
        return super().handle_no_permission()

    def post(self, request, pk, *args, **kwargs):
        session = get_object_or_404(AuditSession.objects.for_scope(Scope.current()), pk=pk, status="active")
        form = AuditBarcodeScanForm(request.POST)

        if form.is_valid():
            barcode = form.cleaned_data["barcode"].strip()

            scan_assets = authorized_scan_assets_queryset(session, user=request.user)
            asset, ambiguous = resolve_scanned_asset_in_queryset(barcode, scan_assets)

            if not asset:
                if ambiguous:
                    message = _("'%(barcode)s' matches several assets. Scan the asset's label instead.") % {
                        "barcode": barcode
                    }
                else:
                    message = _("Scanned asset tag or serial '%(barcode)s' not found in database.") % {
                        "barcode": barcode
                    }
                response = HttpResponse("<div class='alert alert-danger mb-0'>" + str(message) + "</div>", status=400)
                response["HX-Trigger"] = "playAuditFailSound"
                return response

            try:
                audit_asset(
                    asset=asset,
                    user=request.user,
                    session=session,
                    location=session.location or asset.location,
                    status=asset.status,
                    verification_method="barcode",
                )
            except ValidationError as err:
                # Log the specific validation detail server-side; return a generic
                # message so exception text is never reflected to the client.
                logger.warning("Audit scan rejected for asset %s: %s", getattr(asset, "pk", None), err)
                response = HttpResponse(
                    "<div class='alert alert-danger mb-0'>" + str(_("Audit scan could not be recorded.")) + "</div>",
                    status=400,
                )
                response["HX-Trigger"] = "playAuditFailSound"
                return response

            response = render(request, "compliance/audits/includes/audit_scan_success.html", {"asset": asset})
            response["HX-Trigger"] = json.dumps({"updateReconciliation": None, "playAuditSound": None})
            return response

        response = HttpResponse(
            "<div class='alert alert-danger mb-0'>" + str(_("Invalid form submission.")) + "</div>", status=400
        )
        response["HX-Trigger"] = "playAuditFailSound"
        return response


class AuditSessionValidateView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """idempotent, records nothing: given a tag, return classification and details."""

    permission_required = "compliance.add_assetaudit"

    def get(self, request, pk, *args, **kwargs):
        session = get_object_or_404(AuditSession.objects.for_scope(Scope.current()), pk=pk, status="active")
        code = request.GET.get("code", "").strip()
        if not code:
            return JsonResponse({"found": False}, status=400)

        scan_assets = authorized_scan_assets_queryset(session, user=request.user)
        asset, ambiguous = resolve_scanned_asset_in_queryset(code, scan_assets)
        if asset is None:
            payload = {"found": False}
            if ambiguous:
                payload["ambiguous"] = True
            return JsonResponse(payload, status=404)

        expected_ids = set(expected_scan_assets_queryset(session, user=request.user).values_list("id", flat=True))
        observed_location = session.location or asset.location
        eligible, warning, classification = _classify_audit_scan(session, asset, expected_ids, observed_location)

        return JsonResponse(
            {
                "found": True,
                "pk": asset.pk,
                "label": str(asset),
                "asset_tag": asset.asset_tag or "",
                "serial": asset.serial_number or "",
                "status": str(asset.status) if asset.status else "",
                "classification": classification,
                "observed_location": observed_location.name if observed_location else "",
                "eligible": eligible,
                "warning": warning,
            }
        )


class AuditSessionCommitView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Create verifications in one transaction.atomic(), attribute to auditor."""

    permission_required = "compliance.add_assetaudit"

    def post(self, request, pk, *args, **kwargs):
        session = get_object_or_404(AuditSession.objects.for_scope(Scope.current()), pk=pk, status="active")
        asset_pks = request.POST.getlist("pk")

        if not asset_pks:
            # 204 → htmx won't swap (preserves the basket UI); the toast surfaces the error.
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps(
                {
                    "playAuditFailSound": None,
                    "showMessage": {"message": str(_("No assets in basket to commit.")), "level": "danger"},
                }
            )
            return response

        try:
            classified = commit_audit_basket(
                session,
                user=request.user,
                asset_ids=asset_pks,
                request=request,
            )
        except ValidationError as err:
            msg = err.message if hasattr(err, "message") else str(err)
            # 204 → no swap, basket preserved; surface the failure as a toast.
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps(
                {
                    "playAuditFailSound": None,
                    "showMessage": {"message": str(msg), "level": "danger"},
                }
            )
            return response

        # Return the updated reconciliation container using a helper-like template render
        from compliance.forms_audit import AuditBarcodeScanForm

        # Classification is performed inside the atomic write above using the
        # actor-only scan permission, rather than the separate read permission.
        expected_loc_name = session.location.name if session.location else "Global"
        ctx = {
            "total_expected": len(classified["matching"])
            + len(classified["mismatched"])
            + classified["missing"].count(),
            "total_scanned": len(classified["matching"]) + len(classified["mismatched"]) + len(classified["surprise"]),
            "matching": [_audit_to_dict(a, "matching") for a in classified["matching"]],
            "mismatches": [_audit_to_dict(a, "mismatched", expected_loc_name) for a in classified["mismatched"]],
            "surprise_finds": [_audit_to_dict(a, "surprise") for a in classified["surprise"]],
            "missing_assets": [_missing_asset_to_dict(a, session.location) for a in classified["missing"]],
            "report_is_stored": False,
            "object": session,
            "scan_form": AuditBarcodeScanForm(),
        }

        # Basket resets via the #reconciliation-container outerHTML swap — no
        # auditCommitSuccess trigger needed (it would be an orphan event).
        return render(request, "compliance/audits/audit_session_detail.html", ctx)


class AuditSessionCloseForm(TenantScopedFormMixin, forms.Form):
    def __init__(self, *args, **kwargs):
        kwargs.pop("instance", None)
        super().__init__(*args, **kwargs)


class AuditSessionCloseView(GenericTransactionView):
    queryset = AuditSession.objects.filter(status="active")
    model_form = AuditSessionCloseForm
    template_name = "compliance/audits/audit_session_close.html"
    service_callable = close_audit_session
    permission_required = "compliance.change_auditsession"
    success_message = _("Audit session closed. Reconciliation report generated.")

    def get_service_kwargs(self, form):
        return {}


class AuditSessionRehomeView(SimplePostView):
    queryset = AuditSession.objects.filter(status="completed")
    permission_required = "assets.change_asset"

    def perform_action(self, session, request) -> dict:
        result = rehome_audit_session_mismatches(session, user=request.user)
        return {
            "message": _(
                "Rehome complete: %(moved)s moved, %(already)s already correct, "
                "%(conflicted)s changed since close, %(unavailable)s unavailable."
            )
            % {
                "moved": result["moved"],
                "already": result["already_correct"],
                "conflicted": result["conflicted"],
                "unavailable": result["unavailable"],
            }
        }


class AuditSessionReportCsvView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Stream the stored reconciliation report as a CSV download."""

    permission_required = "compliance.view_auditsession"

    def get(self, request, pk, *args, **kwargs):
        session = get_object_or_404(AuditSession.objects.for_scope(Scope.current()), pk=pk, status="completed")
        report = _read_report_or_deny(session, request.user)
        rows = report["rows"]

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="audit-report-{session.pk}.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                "Category",
                "Asset Tag",
                "Asset Name",
                "Observed Location",
                "Expected Location",
                "Auditor",
                "Timestamp",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    csv_safe(row.get("category", "")),
                    csv_safe(row.get("asset_tag", "")),
                    csv_safe(row.get("name", "")),
                    csv_safe(row.get("observed_location", "")),
                    csv_safe(row.get("expected_location", "")),
                    csv_safe(row.get("auditor", "")),
                    csv_safe(row.get("timestamp_display", "") or (row.get("timestamp", "") or "")[:16]),
                ]
            )
        return response


class AuditSessionFlagMissingForm(TenantScopedFormMixin, forms.Form):
    """Empty confirmation form — submit confirms the bulk flag action."""

    pass


class AuditSessionFlagMissingView(GenericTransactionView):
    """Confirm + execute bulk 'Flag missing as Missing' action on completed sessions."""

    queryset = AuditSession.objects.filter(status="completed")
    model_form = AuditSessionFlagMissingForm
    template_name = "compliance/audits/audit_session_flag_missing.html"
    error_partial = "compliance/audits/audit_session_flag_missing.html#flag-missing-form"
    service_callable = flag_missing_assets
    permission_required = "assets.change_asset"
    context_object_name = "session"
    hx_trigger = "tableRefreshRequired"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.pop("instance", None)
        return kwargs

    def get_service_kwargs(self, form):
        return {}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.get_object()
        try:
            context["missing_count"] = preview_missing_assets(session, user=self.request.user)
        except ValidationError as exc:
            raise PermissionDenied(_("The stored audit report is unavailable.")) from exc
        return context

    def get_success_message(self, result=None):
        if result:
            flagged = result["flagged"]
            skipped = result["skipped"]
            flagged_message = ngettext(
                "%(count)s asset flagged as Missing.",
                "%(count)s assets flagged as Missing.",
                flagged,
            ) % {"count": flagged}
            skipped_message = ngettext(
                "%(count)s asset was skipped because its status changed after the audit closed.",
                "%(count)s assets were skipped because their status changed after the audit closed.",
                skipped,
            ) % {"count": skipped}
            return f"{flagged_message} {skipped_message}"
        return _("Missing assets flagged.")


class AuditSessionDeleteView(ObjectDeleteView):
    queryset = AuditSession.objects.all()
    model = AuditSession
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("compliance:auditsession_list")

    def get_object(self, queryset=None):
        session = super().get_object(queryset)
        if session.status == "completed":
            raise PermissionDenied(_("Completed audit sessions and their evidence cannot be deleted."))
        return session


class AuditSessionStartView(SimplePostView):
    """Transition a planned session to active."""

    queryset = AuditSession.objects.filter(status="planned")
    permission_required = "compliance.change_auditsession"

    def perform_action(self, session, request) -> dict:
        from django.core.exceptions import ValidationError

        if session.status != "planned":
            raise ValidationError(_("Only planned sessions can be started."))
        session.status = "active"
        session.save(update_fields=["status"])
        return {"message": _("Campaign '%(name)s' is now active.") % {"name": session.name}}

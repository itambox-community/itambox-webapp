import json
import logging

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db.models import Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _
from django_tables2 import RequestConfig

from assets.choices import RequestStatusChoices
from assets.customfields import (
    composed_fieldsets,
    fetch_definitions_by_name,
    resolve_asset_custom_fields,
    resolve_asset_type_custom_fields,
)
from assets.depreciation import resolve_policy
from assets.models import AssetDisposal, Manufacturer
from assets.services.request_authorization import can_asset_request_action, is_self_service_claim
from assets.services.timeline import build_asset_timeline
from assets.spec_display import build_spec_panel
from compliance.audit_services import audit_asset_from_form
from compliance.models import AssetAudit, CustodyReceipt
from compliance.services import scope_custody_receipts
from compliance.tables import CustodyReceiptTable
from core.managers import Scope
from extras.services.vocabulary_translations import VocabularyTranslator
from inventory.models import AccessoryAssignment, ConsumableAssignment
from inventory.tables import AccessoryAssignmentTable, ConsumableAssignmentTable
from itambox.panels import Panel
from itambox.views.generic import (
    ObjectBulkDeleteView,
    ObjectBulkEditView,
    ObjectCloneView,
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)
from itambox.views.generic.service_views import GenericTransactionView
from licenses.models import LicenseSeatAssignment
from licenses.tables import LicenseSeatAssignmentTable
from organization.models import AssetHolder, ContactAssignment
from software.models import InstalledSoftware
from software.tables import InstalledSoftwareTable

from .. import filters, forms, tables
from ..models import Asset, AssetAssignment, AssetRequest, AssetTypeFieldset
from ..services import checkin_asset, checkout_asset

logger = logging.getLogger(__name__)
User = get_user_model()


class AssetListView(ObjectListView):
    queryset = tables.with_asset_table_relations(Asset.objects.all())

    filterset = filters.AssetFilterSet
    filterset_form = forms.AssetFilterForm
    table = tables.AssetTable
    action_buttons = ("add",)


def _support_contact(asset):
    """``Manufacturer.get_support_contact`` resolved from the prefetched contacts.

    Same precedence and ordering as the model property (support role, then a
    primary contact, then any contact), without three queries per render.
    """
    manufacturer = asset.asset_type.manufacturer if asset.asset_type else None
    if manufacturer is None:
        return None
    assignments = list(manufacturer.contacts.all())
    assignment = (
        next((a for a in assignments if a.role.slug in ("support", "technical-support")), None)
        or next((a for a in assignments if a.priority == "primary"), None)
        or next(iter(assignments), None)
    )
    return assignment.contact if assignment else None


class AssetDetailView(ObjectDetailView):
    queryset = Asset.objects.select_related(
        "asset_role",
        "location",
        "asset_type",
        "asset_type__manufacturer",
        "status",
        "tenant",
        "supplier",
        "cost_center",
        "purchase_order_line__purchase_order",
    ).prefetch_related(
        "tags",
        "maintenances",
        Prefetch(
            "assignments",
            queryset=AssetAssignment.objects.filter(is_active=True).select_related(
                "assigned_user", "assigned_location", "assigned_asset", "checked_out_by"
            ),
            to_attr="prefetched_active_assignments",
        ),
        Prefetch(
            "asset_type__fieldset_memberships",
            queryset=AssetTypeFieldset.objects.select_related("fieldset").prefetch_related(
                "fieldset__field_memberships__custom_field__object_types"
            ),
        ),
        "component_allocations__component",
        "component_allocations__component__manufacturer",
    )
    related_object_exclusions = ("compliance.custodyreceipt",)

    layout = (
        (
            (
                Panel("asset_info", _("Asset Details")),
                Panel("specs", _("Hardware Specifications")),
                Panel("custom_fields", _("Custom Fields")),
            ),
            (
                Panel("assignment", _("Deployment & Custody")),
                Panel("financial", _("Financial & Lifecycle")),
                Panel("audit", _("Audit & Compliance")),
                Panel("support", _("Support & Warranty Details")),
            ),
        ),
    )

    def get_queryset(self):
        # The manufacturer's contacts are shared catalogue rows; only those
        # visible in the request's scope may become the asset's support contact.
        # Built per request: the class attribute is evaluated without a scope.
        contacts = ContactAssignment.objects.visible_in(
            Scope.current(), content_type=ContentType.objects.get_for_model(Manufacturer)
        ).select_related("contact", "role")
        return (
            super().get_queryset().prefetch_related(Prefetch("asset_type__manufacturer__contacts", queryset=contacts))
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        asset = self.get_object()

        active_assignment = asset.active_assignment
        context["assignment"] = active_assignment
        type_fields = resolve_asset_type_custom_fields(asset.asset_type) if asset.asset_type else []
        context["asset_type_specification_fields"] = type_fields
        fieldsets = composed_fieldsets(asset.asset_type)
        own_fields = resolve_asset_custom_fields(asset.asset_type, asset.custom_field_data)
        context["asset_specification_fields"] = own_fields
        # One translator per request: rows for both panels load in one pass.
        translator = VocabularyTranslator(active_locale=get_language())
        translator.load_resolved([*type_fields, *own_fields], fieldsets)
        context["asset_type_spec_panel"] = build_spec_panel(
            type_fields,
            asset.asset_type.custom_field_data if asset.asset_type else {},
            fieldsets,
            include_further=False,
            translator=translator,
        )
        context["asset_spec_panel"] = build_spec_panel(
            own_fields,
            asset.custom_field_data,
            fieldsets,
            definitions_loader=fetch_definitions_by_name,
            translator=translator,
        )

        sw_qs = (
            InstalledSoftware.objects.for_scope(Scope.current())
            .filter(asset=asset)
            .select_related("asset", "software", "software__manufacturer")
        )
        sw_table = InstalledSoftwareTable(sw_qs)
        RequestConfig(self.request, paginate={"per_page": 10}).configure(sw_table)
        context["software_table"] = sw_table

        comp_qs = asset.component_allocations.filter(deleted_at__isnull=True).select_related(
            "component", "component__manufacturer"
        )
        comp_table = tables.ComponentAllocationTable(comp_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": 10}).configure(comp_table)
        context["components_table"] = comp_table

        maint_qs = asset.maintenances.select_related("asset", "supplier")
        maint_table = tables.AssetMaintenanceTable(maint_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": 10}).configure(maint_table)
        context["maintenances_table"] = maint_table

        acc_qs = (
            AccessoryAssignment.objects.for_scope(Scope.current())
            .filter(assigned_asset=asset)
            .select_related("accessory", "accessory__manufacturer")
        )
        acc_table = AccessoryAssignmentTable(acc_qs, request=self.request)
        acc_table.exclude = ("assigned_to",)
        RequestConfig(self.request, paginate={"per_page": 10}).configure(acc_table)
        context["accessory_table"] = acc_table

        con_qs = (
            ConsumableAssignment.objects.for_scope(Scope.current())
            .filter(assigned_asset=asset)
            .select_related("consumable", "consumable__manufacturer")
        )
        con_table = ConsumableAssignmentTable(con_qs, request=self.request)
        con_table.exclude = ("assigned_to",)
        RequestConfig(self.request, paginate={"per_page": 10}).configure(con_table)
        context["consumable_table"] = con_table

        # Requests

        req_qs = (
            AssetRequest.objects.for_scope(Scope.current())
            .filter(asset=asset)
            .select_related("requester", "asset", "asset_type")
        )
        requests_table = tables.AssetRequestTable(req_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": 10}).configure(requests_table)
        context["requests_table"] = requests_table

        # Audits

        audit_qs = AssetAudit.objects.filter(asset=asset).select_related("session", "auditor", "location", "status")
        audits_table = tables.AssetAuditTable(audit_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": 10}).configure(audits_table)
        context["audits_table"] = audits_table

        # Custody Receipts

        can_view_receipts = self.request.user.has_perm("compliance.view_custodyreceipt", obj=asset)
        context["can_view_custody_receipts"] = can_view_receipts
        receipt_qs = CustodyReceipt.objects.none()
        if can_view_receipts:
            receipt_qs = scope_custody_receipts(
                CustodyReceipt.objects.filter(asset=asset, asset__tenant=asset.tenant).select_related(
                    "asset", "holder", "custody_template"
                ),
                user=self.request.user,
                permission="compliance.view_custodyreceipt",
            )
            receipts_table = CustodyReceiptTable(receipt_qs, request=self.request)
            RequestConfig(self.request, paginate={"per_page": 10}).configure(receipts_table)
            context["receipts_table"] = receipts_table

        # License Seat Assignments

        license_qs = (
            LicenseSeatAssignment.objects.for_scope(Scope.current())
            .filter(asset=asset)
            .select_related("license", "asset", "assigned_holder")
            .prefetch_related(
                Prefetch(
                    "asset__assignments",
                    queryset=AssetAssignment.objects.filter(is_active=True).select_related(
                        "assigned_user", "assigned_location", "assigned_asset"
                    ),
                    to_attr="prefetched_active_assignments",
                )
            )
        )
        license_seats_table = LicenseSeatAssignmentTable(license_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": 10}).configure(license_seats_table)
        context["license_seats_table"] = license_seats_table

        context["is_modular"] = bool(asset.component_allocations.all()) or bool(
            asset.asset_role and asset.asset_role.allows_components
        )
        context["current_warranty_end"] = asset.current_warranty_end
        context["support_contact"] = _support_contact(asset)
        # Badge counts reuse the tab tables' paginator counts, so each badge matches
        # the rows its tab shows and costs no extra COUNT query.
        context["tab_counts"] = {
            "software": sw_table.paginator.count,
            "requests": requests_table.paginator.count,
            "audits": audits_table.paginator.count,
            "licenses": license_seats_table.paginator.count,
        }

        context["eol_date"] = asset.eol_date
        context["eol_expired"] = asset.eol_date is not None and timezone.localdate() >= asset.eol_date
        context["time_to_eol"] = asset.time_to_eol
        context["total_cost_of_ownership"] = asset.total_cost_of_ownership

        policy, rung = resolve_policy(asset)
        context["resolved_depreciation_policy"] = str(policy) if policy else None
        context["resolved_depreciation_rung"] = rung

        if can_view_receipts and active_assignment and isinstance(active_assignment.assigned_target, AssetHolder):
            current_receipt = next(
                (
                    receipt
                    for receipt in receipt_qs.filter(holder=active_assignment.assigned_target).order_by(
                        "-created_date", "-pk"
                    )
                    if receipt.assignment_id in (None, active_assignment.pk)
                ),
                None,
            )
            if current_receipt is not None:
                context["custody_receipt_summary"] = {
                    "acceptance_status": current_receipt.acceptance_status,
                    "detail_url": reverse("compliance:custodyreceipt_detail", kwargs={"pk": current_receipt.pk}),
                }

        receipt_list_url = reverse("compliance:custodyreceipt_list")
        context["related_objects_list"] = [
            item for item in context.get("related_objects_list", []) if not item["url"].startswith(receipt_list_url)
        ]
        receipt_count = context["receipts_table"].paginator.count if can_view_receipts else 0
        if receipt_count:
            context["related_objects_list"].append(
                {
                    "label": str(_("Custody Receipts")),
                    "count": receipt_count,
                    "url": f"{receipt_list_url}?asset={asset.pk}",
                }
            )
            context["related_objects_list"].sort(key=lambda item: item["label"])

        # Check if current user can claim an approved request for this asset
        approved_request = None
        if self.request.user.is_authenticated:
            approved_request_qs = AssetRequest.objects.for_scope(Scope.current()).filter(
                asset=asset, status=RequestStatusChoices.APPROVED
            )
            for req in approved_request_qs:
                if can_asset_request_action(self.request.user, req, "claim"):
                    approved_request = req
                    break
        context["approved_request"] = approved_request
        context["claim_is_self_service"] = approved_request is not None and is_self_service_claim(
            self.request.user, approved_request
        )

        warranty_qs = asset.warranties.select_related("asset")
        warranties_table = tables.WarrantyTable(warranty_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": 10}).configure(warranties_table)
        context["warranties_table"] = warranties_table

        reservation_qs = asset.reservations.select_related("asset", "reserved_for", "created_by")
        reservations_table = tables.AssetReservationTable(reservation_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": 10}).configure(reservations_table)
        context["reservations_table"] = reservations_table
        context["tab_counts"]["warranties"] = warranties_table.paginator.count
        context["tab_counts"]["reservations"] = reservations_table.paginator.count

        # Local-import debt: hoisting this lands in the second import block below
        # `logger = ...`, which is pre-existing E402 debt. Hoist it with that cleanup.

        # Disposal is a history, not a single one-to-one record. The active
        # record owns the life cycle state; cancelled records stay visible.
        # A soft-deleted record that was never cancelled still owns the
        # lifecycle, so the history must include tombstones through the tenant-safe
        # manager (``all_objects``) - otherwise the UI claims "no record" and offers a
        # disposal the service then rejects.
        # unscoped: disposal and reservation evidence includes cancelled and soft-deleted rows; asset scope narrows
        # visibility
        disposal_history = list(
            AssetDisposal.all_objects.filter(asset=asset)
            .select_related("cancelled_by")
            .order_by("-disposal_date", "-pk")
        )
        context["disposal_history"] = disposal_history
        disposal_obj = next((disposal for disposal in disposal_history if disposal.is_active), None)
        context["disposal_obj"] = disposal_obj
        # Same rows ``Asset.active_disposal`` reads (all_objects, uncancelled); the
        # documented prefetch hook spares the panels a query per access.
        asset.prefetched_active_disposals = [disposal_obj] if disposal_obj is not None else []

        # The repair timeline groups the records that belong to a repair
        # maintenance and falls back to chronological order otherwise.
        timeline = build_asset_timeline(asset)
        context["asset_timeline"] = timeline
        context["recent_activity"] = timeline.recent_events

        return context


class AssetEditView(ObjectEditView):
    queryset = Asset.objects.all()
    model = Asset
    model_form = forms.AssetForm
    template_name = "assets/asset_specification_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def post(self, request, *args, **kwargs):
        if request.headers.get("HX-Request") and "_reload" in request.POST:
            self.object = self.get_object()
            form = self.get_form()
            context = self.get_context_data(form=form)
            return render(request, "assets/_asset_specification_form.html", context)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        try:
            response = super().form_valid(form)
        except ValidationError as exc:
            form.add_error(None, exc)
            form.specification_revision_conflict = True
            return self.form_invalid(form)
        if getattr(self, "object", None) is not None:
            form.create_inline_warranty(self.object)
        return response


class AssetDeleteView(ObjectDeleteView):
    queryset = Asset.objects.all()
    model = Asset
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("assets:asset_list")


class AssetCloneView(ObjectCloneView):
    model = Asset
    model_form = forms.AssetForm
    template_name = "assets/asset_specification_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def pre_save_clone(self, original, cloned):
        cloned.asset_tag = ""


class AssetCheckoutView(GenericTransactionView):
    permission_required = ("assets.change_asset",)
    queryset = Asset.objects.all()
    model_form = forms.AssetCheckOutForm
    service_callable = checkout_asset
    context_object_name = "asset"
    template_name = "assets/includes/asset_checkout_modal.html"
    error_partial = "assets/includes/asset_checkout_modal.html#checkout-modal-form"
    success_message = "Asset checked out successfully."
    hx_trigger = "assetListUpdated"
    hx_redirect_on_success = True
    form_field_map = {"asset_holder": "holder"}
    form_exclude_fields = ("target_type",)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        del kwargs["instance"]
        asset = self.get_object()
        kwargs["asset"] = asset

        request_id = self.request.GET.get("request_id")
        if request_id:
            asset_request = AssetRequest.objects.for_scope(Scope.current()).filter(pk=request_id).first()
            if asset_request is not None:
                if "initial" not in kwargs:
                    kwargs["initial"] = {}
                initial = kwargs["initial"]
                if asset_request.assigned_user:
                    initial["target_type"] = "holder"
                    initial["asset_holder"] = asset_request.assigned_user
                elif asset_request.assigned_location:
                    initial["target_type"] = "location"
                    initial["location"] = asset_request.assigned_location
                elif asset_request.assigned_asset:
                    initial["target_type"] = "asset"
                    initial["asset_target"] = asset_request.assigned_asset

        return kwargs

    def get_success_message(self, result=None):
        return f"Asset '{self.get_object()}' checked out to {result}."

    def post_service(self, obj, form, result):
        # Fulfill the originating request, but only when it actually references
        # this asset (or this asset's type) — never an arbitrary request id.
        request_id = self.request.GET.get("request_id")
        if not request_id:
            return

        request_filter = Q(asset=obj)
        if obj.asset_type_id:
            request_filter |= Q(asset__isnull=True, asset_type_id=obj.asset_type_id)
        asset_request = (
            AssetRequest.objects.for_scope(Scope.current())
            .filter(
                pk=request_id,
                status__in=(RequestStatusChoices.PENDING, RequestStatusChoices.APPROVED),
            )
            .filter(request_filter)
            .first()
        )
        if asset_request:
            asset_request.status = RequestStatusChoices.FULFILLED
            asset_request.response_date = timezone.now()
            asset_request.responded_by = self.request.user
            asset_request.asset = obj
            asset_request.save(update_fields=["status", "response_date", "responded_by", "asset"])


class AssetCheckinView(GenericTransactionView):
    permission_required = ("assets.change_asset",)
    queryset = Asset.objects.all()
    model_form = forms.AssetCheckInForm
    service_callable = checkin_asset
    context_object_name = "asset"
    template_name = "assets/includes/asset_checkin_modal.html"
    error_partial = "assets/includes/asset_checkin_modal.html#checkin-modal-form"
    success_message = "Asset checked in successfully."
    hx_trigger = "assetListUpdated"
    hx_redirect_on_success = True

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        del kwargs["instance"]
        kwargs["asset"] = self.get_object()
        return kwargs

    def get_success_message(self, result=None):
        return f"Asset '{self.get_object()}' checked in successfully."


class AssetAuditView(GenericTransactionView):
    """Modal form for standalone asset verification from the detail page."""

    queryset = Asset.objects.all()
    permission_required = "compliance.add_assetaudit"
    context_object_name = "asset"
    template_name = "assets/includes/asset_audit_modal.html"
    error_partial = "assets/includes/asset_audit_modal.html#audit-modal-form"
    success_message = "Asset physically verified."
    hx_trigger = "assetAuditRecorded"

    model_form = forms.AssetAuditConfirmForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.pop("instance", None)
        kwargs["asset"] = self.get_object()
        return kwargs

    def get_service_kwargs(self, form):
        return {
            "location": form.cleaned_data["location"],
            "status": form.cleaned_data["status"],
            "notes": form.cleaned_data.get("notes", ""),
        }

    def _htmx_success_response(self, obj, result=None):
        """Return OOB-swapped badge + closeModal + playAuditSound triggers."""
        badge_html = render(
            self.request,
            "assets/includes/asset_audit_badge.html",
            {"asset": obj},
        ).content.decode()
        oob_fragment = (
            f'<div id="asset-audit-badge-container" hx-swap-oob="outerHTML:#asset-audit-badge-container">'
            f"{badge_html}</div>"
        )
        response = HttpResponse(oob_fragment, content_type="text/html")
        response["HX-Trigger"] = json.dumps(
            {
                "closeModalEvent": None,
                "playAuditSound": None,
                "showMessage": {
                    "message": str(self.get_success_message(result)),
                    "level": "success",
                },
            }
        )
        return response

    def get_success_message(self, result=None):
        asset = self.get_object()
        if result and result.get("session"):
            return _("'%(asset)s' verified inside campaign '%(campaign)s'.") % {
                "asset": asset.name,
                "campaign": result["session"].name,
            }
        return _("'%(asset)s' standalone verification recorded.") % {"asset": asset.name}

    service_callable = audit_asset_from_form


class AssetBulkEditView(ObjectBulkEditView):
    queryset = Asset.objects.all()
    form_class = forms.AssetBulkEditForm


class AssetBulkDeleteView(ObjectBulkDeleteView):
    queryset = Asset.objects.all()

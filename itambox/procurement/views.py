import json
import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.forms import formset_factory
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from assets.models import AssetRequest
from core.managers import Scope
from itambox.views.generic import ObjectDeleteView, ObjectDetailView, ObjectEditView, ObjectListView
from itambox.views.generic.service_views import SimplePostView
from procurement.models import FulfillmentLink

from .filters import ContractFilterSet, PurchaseOrderFilterSet
from .forms import (
    AssetProvisionForm,
    BaseAssetProvisionFormSet,
    ContractFilterForm,
    ContractForm,
    PurchaseOrderFilterForm,
    PurchaseOrderForm,
    PurchaseOrderLineForm,
    ReceiveLineFormSet,
)
from .models import Contract, PurchaseOrder, PurchaseOrderLine
from .services import (
    approve_purchase_order,
    cancel_purchase_order,
    link_asset_request_to_purchase_order,
    order_purchase_order,
    receive_purchase_order,
    reopen_purchase_order,
)
from .tables import ContractTable, PurchaseOrderTable

logger = logging.getLogger(__name__)


class PurchaseOrderListView(ObjectListView):
    queryset = PurchaseOrder.objects.select_related("supplier", "destination_location")
    filterset = PurchaseOrderFilterSet
    filterset_form = PurchaseOrderFilterForm
    table = PurchaseOrderTable
    template_name = "procurement/purchaseorder_list.html"
    permission_required = "procurement.view_purchaseorder"


class PurchaseOrderDetailView(ObjectDetailView):
    queryset = PurchaseOrder.objects.all()
    template_name = "procurement/purchaseorder_detail.html"
    permission_required = "procurement.view_purchaseorder"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        lines = self.object.lines.all()
        context["lines"] = lines
        context["line_form"] = PurchaseOrderLineForm()

        # Calculate total value of the PO
        total_value = Decimal("0.00")
        for line in lines:
            if line.unit_price and line.qty_ordered:
                total_value += line.unit_price * line.qty_ordered
        context["total_value"] = total_value

        # Fetch linked fulfillment links

        context["fulfillment_links"] = (
            FulfillmentLink.objects.for_scope(Scope.current())
            .filter(purchase_order_line__purchase_order=self.object)
            .select_related("asset_request", "purchase_order_line")
        )

        return context


class PurchaseOrderEditView(ObjectEditView):
    queryset = PurchaseOrder.objects.all()
    model_form = PurchaseOrderForm
    permission_required = "procurement.change_purchaseorder"

    def get_initial(self):
        initial = super().get_initial()
        if "from_request" in self.request.GET:
            try:
                from_request_id = int(self.request.GET.get("from_request"))
            except (TypeError, ValueError) as exc:
                raise Http404(_("Invalid Asset Request identifier.")) from exc

            try:
                asset_request = AssetRequest.objects.for_scope(Scope.current()).get(pk=from_request_id)
                if not self.request.user.has_perm("assets.fulfill_assetrequest", asset_request):
                    raise PermissionDenied(_("You do not have permission to fulfill this Asset Request."))
                loc = asset_request.assigned_location or asset_request.source_location
                if loc:
                    initial["destination_location"] = loc.pk
            except AssetRequest.DoesNotExist:
                pass
        return initial

    def add_success_message(self, message):
        if getattr(self, "_defer_success_message", False):
            self._deferred_success_message = message
            return
        super().add_success_message(message)

    def form_valid(self, form):
        is_creating = self.get_object() is None
        self._defer_success_message = is_creating and "from_request" in self.request.GET
        self._deferred_success_message = None
        linked_request_id = None
        if is_creating:
            form.instance.created_by = self.request.user
        try:
            with transaction.atomic():
                response = super().form_valid(form)
                if is_creating and "from_request" in self.request.GET:
                    from_request_id = self.request.GET.get("from_request")
                    link_asset_request_to_purchase_order(
                        self.object,
                        from_request_id,
                        user=self.request.user,
                    )
                    linked_request_id = int(from_request_id)
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)
        if self._deferred_success_message is not None:
            super().add_success_message(self._deferred_success_message)
        if linked_request_id is not None:
            messages.success(
                self.request,
                _(
                    "Linked Purchase Order to Asset Request #%(pk)s and transitioned request status "
                    "to Awaiting Procurement."
                )
                % {"pk": linked_request_id},
            )
        return response


class PurchaseOrderDeleteView(ObjectDeleteView):
    queryset = PurchaseOrder.objects.all()
    permission_required = "procurement.delete_purchaseorder"
    default_return_url = "procurement:purchaseorder_list"


class PurchaseOrderLineAddView(PermissionRequiredMixin, View):
    permission_required = "procurement.change_purchaseorder"

    def get(self, request, *args, **kwargs):
        po = get_object_or_404(PurchaseOrder.objects.for_scope(Scope.current()), pk=kwargs.get("po_pk"))
        context = {
            "object": po,
            "lines": po.lines.all(),
            "line_form": PurchaseOrderLineForm(),
        }
        return render(request, "procurement/includes/purchaseorder_lines_container.html", context)

    def post(self, request, *args, **kwargs):
        po = get_object_or_404(PurchaseOrder.objects.for_scope(Scope.current()), pk=kwargs.get("po_pk"))
        if po.status != PurchaseOrder.STATUS_DRAFT:
            messages.error(request, _("Line items can only be added while the purchase order is in Draft status."))
            return redirect(po.get_absolute_url())
        form = PurchaseOrderLineForm(request.POST)
        if form.is_valid():
            line = form.save(commit=False)
            line.purchase_order = po
            # Handle tenant scoping if applicable
            if po.tenant:
                line.tenant = po.tenant
            line.save()
            messages.success(request, _("Line item added successfully."))
            form = PurchaseOrderLineForm()
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field}: {error}")

        if request.headers.get("HX-Request"):
            context = {
                "object": po,
                "lines": po.lines.all(),
                "line_form": form,
            }
            return render(request, "procurement/includes/purchaseorder_lines_container.html", context)
        return redirect(po.get_absolute_url())


class PurchaseOrderLineDeleteView(ObjectDeleteView):
    queryset = PurchaseOrderLine.objects.all()
    permission_required = "procurement.change_purchaseorder"

    def get_permission_required(self):
        return (self.permission_required,)

    def get_return_url(self, request, obj):
        return obj.purchase_order.get_absolute_url()

    def post(self, request, *args, **kwargs):
        if request.headers.get("HX-Request"):
            obj = self.get_object()
            po = obj.purchase_order
            if po.status != PurchaseOrder.STATUS_DRAFT:
                messages.error(
                    request, _("Line items can only be removed while the purchase order is in Draft status.")
                )
                context = {
                    "object": po,
                    "lines": po.lines.all(),
                    "line_form": PurchaseOrderLineForm(),
                }
                return render(request, "procurement/includes/purchaseorder_lines_container.html", context)
            obj.delete()
            messages.success(request, _("Line item deleted successfully."))
            context = {
                "object": po,
                "lines": po.lines.all(),
                "line_form": PurchaseOrderLineForm(),
            }
            return render(request, "procurement/includes/purchaseorder_lines_container.html", context)
        return super().post(request, *args, **kwargs)


def _parse_line_values(data):
    """Return ``(qty_ordered, unit_price)`` from a line edit submission, or ``None`` when invalid.

    A blank price means "no price". Decimal accepts NaN and Infinity, which no price can be.
    """
    try:
        qty_ordered = int(data.get("qty_ordered", 1))
        raw_price = (data.get("unit_price") or "").strip()
        unit_price = Decimal(raw_price) if raw_price else None
    except (ValueError, InvalidOperation):
        return None
    if qty_ordered < 1:
        return None
    if unit_price is not None and (not unit_price.is_finite() or unit_price < 0):
        return None
    return qty_ordered, unit_price


class PurchaseOrderLineEditView(PermissionRequiredMixin, View):
    permission_required = "procurement.change_purchaseorder"

    def get(self, request, *args, **kwargs):
        line = get_object_or_404(PurchaseOrderLine.objects.for_scope(Scope.current()), pk=kwargs.get("pk"))
        po = line.purchase_order
        context = {
            "object": po,
            "lines": po.lines.all(),
            "line_form": PurchaseOrderLineForm(),
            "editing_line_id": line.pk,
        }
        return render(request, "procurement/includes/purchaseorder_lines_container.html", context)

    def post(self, request, *args, **kwargs):
        line = get_object_or_404(PurchaseOrderLine.objects.for_scope(Scope.current()), pk=kwargs.get("pk"))
        po = line.purchase_order

        if po.status != PurchaseOrder.STATUS_DRAFT:
            messages.error(request, _("Line items can only be edited while the purchase order is in Draft status."))
            context = {
                "object": po,
                "lines": po.lines.all(),
                "line_form": PurchaseOrderLineForm(),
            }
            return render(request, "procurement/includes/purchaseorder_lines_container.html", context)

        values = _parse_line_values(request.POST)
        if values is None:
            messages.error(request, _("Enter a whole quantity of at least 1 and a unit price that is not negative."))
        else:
            line.qty_ordered, line.unit_price = values
            line.save()
            messages.success(request, _("Line item updated successfully."))

        context = {
            "object": po,
            "lines": po.lines.all(),
            "line_form": PurchaseOrderLineForm(),
        }
        return render(request, "procurement/includes/purchaseorder_lines_container.html", context)


class PurchaseOrderReceiveView(SimplePostView):
    queryset = PurchaseOrder.objects.all()
    permission_required = "procurement.receive_purchaseorder"

    def perform_action(self, obj, request):
        return {"url": reverse("procurement:purchaseorder_receive_form", kwargs={"pk": obj.pk})}

    def _htmx_success_response(self, obj, result):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = result["url"]
        return response

    def get_success_redirect(self, obj, result):
        if result and "url" in result:
            return redirect(result["url"])
        return super().get_success_redirect(obj, result)


class PurchaseOrderTransitionView(SimplePostView):
    def _htmx_success_response(self, obj, result):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = obj.get_absolute_url()
        messages.success(self.request, result.get("message", _("Action completed successfully.")))
        return response


class PurchaseOrderApproveView(PurchaseOrderTransitionView):
    queryset = PurchaseOrder.objects.all()
    permission_required = "procurement.approve_purchaseorder"

    def perform_action(self, obj, request):
        return approve_purchase_order(obj, user=request.user, request=request)


class PurchaseOrderOrderView(PurchaseOrderTransitionView):
    queryset = PurchaseOrder.objects.all()
    permission_required = "procurement.change_purchaseorder"

    def perform_action(self, obj, request):
        return order_purchase_order(obj, user=request.user, request=request)


class PurchaseOrderCancelView(PurchaseOrderTransitionView):
    queryset = PurchaseOrder.objects.all()
    permission_required = "procurement.change_purchaseorder"

    def perform_action(self, obj, request):
        return cancel_purchase_order(obj, user=request.user, request=request)


class PurchaseOrderReopenView(PurchaseOrderTransitionView):
    queryset = PurchaseOrder.objects.all()
    permission_required = "procurement.change_purchaseorder"

    def perform_action(self, obj, request):
        return reopen_purchase_order(obj, user=request.user, request=request)


class PurchaseOrderReceiveFormView(ObjectDetailView):
    queryset = PurchaseOrder.objects.all()
    template_name = "procurement/purchaseorder_receive.html"
    permission_required = "procurement.receive_purchaseorder"

    def get_permission_required(self):
        return (self.permission_required,)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        po = self.get_object()

        # Bind the receipt-state snapshot into this rendered form: each submission carries its
        # own copy of the state it was prepared against, and processing never regenerates it
        # (a second render would otherwise silently revalidate an older, replayed submission).
        context["expected_received_json"] = json.dumps({str(line.pk): line.qty_received for line in po.lines.all()})

        # Prepare initial data for Step 1 formset
        initial_data = []
        outstanding_lines = []
        for line in po.lines.all():
            if line.qty_outstanding > 0:
                initial_data.append({"line_id": line.pk, "qty_to_receive": line.qty_outstanding})
                outstanding_lines.append(line)

        formset = ReceiveLineFormSet(initial=initial_data)

        context["formset"] = formset
        context["lines_and_forms"] = list(zip(outstanding_lines, formset, strict=False))
        context["step"] = "1"
        return context

    def post(self, request, *args, **kwargs):
        po = self.get_object()
        self.object = po
        step = request.POST.get("step", "1")

        if step == "2":
            # Submitting Step 2 (Asset Provisioning)
            line_quantities = request.session.get("receive_po_quantities", {})
            line_quantities = {int(k): int(v) for k, v in line_quantities.items()}
            # The snapshot travels with this form submission; the service refuses the
            # submission if any line moved on since it was prepared (replay protection).
            expected_received = self._parse_expected_received(request)

            # Count the total number of physical assets being received to initialize the formset correctly
            total_assets = 0
            for lid, qty in line_quantities.items():
                line = po.lines.get(pk=lid)
                if line.asset_type:
                    total_assets += qty

            DynamicAssetProvisionFormSet = formset_factory(
                AssetProvisionForm, formset=BaseAssetProvisionFormSet, extra=total_assets
            )
            formset = DynamicAssetProvisionFormSet(request.POST)

            if formset.is_valid():
                return self._receive_and_redirect(
                    request,
                    po,
                    line_quantities,
                    formset.cleaned_data,
                    expected_received,
                    _("Stock received and assets provisioned successfully."),
                    clear_session=True,
                )
            else:
                # Render step 2 again with errors
                lines_info = []
                for form in formset:
                    try:
                        line_id = form["line_id"].value()
                        if line_id:
                            line = po.lines.get(pk=int(line_id))
                            lines_info.append(line)
                        else:
                            lines_info.append(None)
                    except (ValueError, PurchaseOrderLine.DoesNotExist):
                        lines_info.append(None)

                context = self.get_context_data(object=po)
                context["formset"] = formset
                context["lines_info"] = list(zip(lines_info, formset, strict=False))
                context["step"] = "2"
                # Keep this operation's submitted snapshot: a corrected submission stays bound
                # to the state it was prepared against and is refused if that state moved on.
                context["expected_received_json"] = json.dumps({str(pk): qty for pk, qty in expected_received.items()})
                return render(request, "procurement/purchaseorder_receive_step2.html", context)

        else:
            # Submitting Step 1 (Quantities)
            formset = ReceiveLineFormSet(request.POST)
            if formset.is_valid():
                line_quantities = {}
                for form in formset:
                    line_quantities[form.cleaned_data["line_id"]] = form.cleaned_data["qty_to_receive"]

                # The snapshot travels with this form submission; processing never regenerates
                # it from the database, so a replayed submission is refused.
                line_expected = self._parse_expected_received(request)

                # Check if any asset lines are being received
                has_assets = False
                step2_initial = []
                for lid, qty in line_quantities.items():
                    if qty > 0:
                        line = po.lines.get(pk=lid)
                        if line.asset_type:
                            has_assets = True
                            # Generate asset tag previews
                            from assets.models import Asset, AssetTagSequence

                            dummy_asset = Asset(tenant=po.tenant, asset_type=line.asset_type)
                            seq = AssetTagSequence.resolve_sequence_for_asset(dummy_asset)
                            for i in range(qty):
                                tag_preview = f"{seq.prefix}{seq.next_value + i:0{seq.zero_padding}d}"
                                step2_initial.append(
                                    {"line_id": line.pk, "asset_tag": tag_preview, "name": str(line.asset_type)}
                                )

                if has_assets:
                    # Save quantities for step 2; the receipt-state snapshot travels with each
                    # form submission instead of a shared session slot.
                    request.session["receive_po_quantities"] = line_quantities

                    # Initialize step 2 formset
                    DynamicAssetProvisionFormSet = formset_factory(
                        AssetProvisionForm, formset=BaseAssetProvisionFormSet, extra=len(step2_initial)
                    )
                    step2_formset = DynamicAssetProvisionFormSet(initial=step2_initial)

                    lines_info = []
                    for init_data in step2_initial:
                        line = po.lines.get(pk=init_data["line_id"])
                        lines_info.append(line)

                    context = self.get_context_data(object=po)
                    context["formset"] = step2_formset
                    context["lines_info"] = list(zip(lines_info, step2_formset, strict=False))
                    context["step"] = "2"
                    # Carry this operation's snapshot into the step-2 form so its own submission
                    # stays bound to the same prepared state.
                    context["expected_received_json"] = json.dumps({str(pk): qty for pk, qty in line_expected.items()})
                    return render(request, "procurement/purchaseorder_receive_step2.html", context)
                else:
                    # Call receiving service directly (only non-asset inventory items)
                    return self._receive_and_redirect(
                        request, po, line_quantities, None, line_expected, _("Stock received successfully.")
                    )
            else:
                # Step 1 invalid, render again with errors
                outstanding_lines = []
                for form in formset:
                    try:
                        line_id = form["line_id"].value()
                        if line_id:
                            outstanding_lines.append(po.lines.get(pk=int(line_id)))
                        else:
                            outstanding_lines.append(None)
                    except (ValueError, PurchaseOrderLine.DoesNotExist):
                        outstanding_lines.append(None)

                context = self.get_context_data(object=po)
                context["formset"] = formset
                context["lines_and_forms"] = list(zip(outstanding_lines, formset, strict=False))
                context["step"] = "1"
                return render(request, "procurement/purchaseorder_receive.html", context)

    def _receive_and_redirect(
        self, request, po, line_quantities, asset_details, expected_received, success_message, *, clear_session=False
    ):
        """Book the receipt and report the outcome. Unexpected errors propagate and are logged by Django."""
        try:
            receive_purchase_order(po, line_quantities, asset_details, expected_received=expected_received)
        except ValidationError as exc:
            # The service's own refusals (stale snapshot, over-receipt, wrong status) are written for users.
            messages.error(request, _("Error receiving purchase order: %(error)s") % {"error": " ".join(exc.messages)})
        except IntegrityError:
            # The step-2 form checks active assets only, but asset tags stay reserved by deleted
            # ones, and a concurrent receipt can claim a tag or serial between validation and save.
            # The database is the last line; the receipt has rolled back.
            logger.warning("Receipt for purchase order %s refused by a database constraint", po.pk, exc_info=True)
            messages.error(
                request,
                _(
                    "Nothing was received: an asset tag or serial number is already in use. Asset tags stay "
                    "reserved by deleted assets. Change the values and submit again."
                ),
            )
        else:
            if clear_session:
                request.session.pop("receive_po_quantities", None)
            messages.success(request, success_message)
        return redirect(po.get_absolute_url())

    def _parse_expected_received(self, request):
        """The receipt-state snapshot a receipt submission itself carries.

        The snapshot is bound into every rendered form, so a submission always carries the
        state it was prepared against: a later render, another tab, or another order can
        never revalidate an older, replayed submission, and processing never regenerates
        the snapshot from the database.
        """
        try:
            raw = request.POST.get("expected_received", "")
            state = json.loads(raw) if raw else {}
            return {int(key): int(value) for key, value in state.items()}
        except (AttributeError, TypeError, ValueError):
            return {}


# ---------------------------------------------------------------------------
# Contract views
# ---------------------------------------------------------------------------


class ContractListView(ObjectListView):
    queryset = Contract.objects.select_related("supplier")
    filterset = ContractFilterSet
    filterset_form = ContractFilterForm
    table = ContractTable
    template_name = "procurement/contract_list.html"
    permission_required = "procurement.view_contract"


class ContractDetailView(ObjectDetailView):
    queryset = Contract.objects.all()
    template_name = "procurement/contract_detail.html"
    permission_required = "procurement.view_contract"

    def get_object_display(self, obj):
        return f"{obj.contract_number}: {obj.name}"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        contract = self.object
        context["covered_assets"] = contract.assets.select_related("asset_type", "tenant")
        return context


class ContractEditView(ObjectEditView):
    queryset = Contract.objects.all()
    model_form = ContractForm
    permission_required = "procurement.change_contract"


class ContractDeleteView(ObjectDeleteView):
    queryset = Contract.objects.all()
    permission_required = "procurement.delete_contract"
    default_return_url = "procurement:contract_list"

    def get_object_display(self):
        return f"{self.object.contract_number}: {self.object.name}"

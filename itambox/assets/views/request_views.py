from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django.views import View

from assets import filters, tables
from assets.choices import RequestStatusChoices
from assets.forms.request_forms import (
    AssetReceiveFormSet,
    AssetRequestActionForm,
    AssetRequestForm,
    AssetRequestManualCompletionForm,
)
from assets.models import Asset, AssetAssignment, AssetRequest, AssetTagSequence, StatusLabel
from assets.services import checkout_asset
from assets.services.request_authorization import can_asset_request_action, is_self_service_claim
from assets.services.request_fulfillment import (
    checkout_transaction_reference,
    claim_fulfillment_scope,
    complete_request_from_checkout,
    get_request_fulfillment_evidence,
    manually_complete_request,
    request_fulfillment_labels,
)
from core.managers import Scope
from inventory.services import checkout_inventory_item
from itambox.panels import Panel
from itambox.views.generic import (
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)
from itambox.views.generic.service_views import GenericTransactionView, SimplePostView
from organization.rbac import build_accessible_tenant_permissions_map
from procurement.models import FulfillmentLink, PurchaseOrderLine
from procurement.services import lock_unit_fulfillment_links, release_fulfillment_links


def _claim_handover_note(actor, req) -> str:
    """Audit note for a recorded claim handover.

    A self-service actor claims their own request; a scoped fulfilment actor
    records the handover on the target's behalf.
    """
    if is_self_service_claim(actor, req):
        return f"Self-service claim for approved Request #{req.pk}"
    return f"Fulfillment handover for approved Request #{req.pk}"


def _request_action_tenant_ids(user):
    """Return tenants where the user may approve or fulfill asset requests."""
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return set()

    permission_map = build_accessible_tenant_permissions_map(user)
    request_permissions = {"assets.approve_assetrequest", "assets.fulfill_assetrequest"}
    return {
        tenant_id
        for tenant_id, (permissions, _valid_until) in permission_map.items()
        if request_permissions.intersection(permissions)
    }


# --- Service Layer Callables defined inside the View boundaries ---


@transaction.atomic
def approve_asset_request(request_instance, user, request=None, **kwargs):
    request_instance = AssetRequest.objects.for_scope(Scope.current()).select_for_update().get(pk=request_instance.pk)
    if request_instance.status != RequestStatusChoices.PENDING:
        raise ValidationError(_("Only pending requests can be approved."))

    asset = kwargs.get("allocated_asset")
    if asset:
        if not asset.is_requestable:
            raise ValidationError(_("Allocated asset '%(name)s' is not marked as requestable.") % {"name": asset.name})
        if not asset.is_issuable:
            raise ValidationError(_("The selected asset is not available for issuance."))
        if request_instance.asset_type and asset.asset_type != request_instance.asset_type:
            raise ValidationError(_("Allocated asset does not match the requested asset type."))
        request_instance.asset = asset

    qty = kwargs.get("qty")
    if request_instance.component or request_instance.accessory or request_instance.consumable:
        if qty is not None:
            if qty <= 0:
                raise ValidationError(_("Quantity must be greater than zero."))
            if qty > request_instance.qty:
                raise ValidationError(_("Quantity cannot exceed requested quantity."))
            request_instance.qty = qty

    allocated_location = kwargs.get("allocated_location")
    if allocated_location:
        request_instance.source_location = allocated_location

    request_instance.status = RequestStatusChoices.APPROVED
    request_instance.responded_by = user
    request_instance.response_date = timezone.now()
    request_instance.response_notes = kwargs.get("response_notes", "")
    request_instance.save()

    if request_instance.is_group:
        for child in request_instance.sub_requests.filter(status=RequestStatusChoices.PENDING):
            child.status = RequestStatusChoices.APPROVED
            child.responded_by = user
            child.response_date = timezone.now()
            child.response_notes = request_instance.response_notes
            if allocated_location:
                child.source_location = allocated_location
            child.save()

    return request_instance


@transaction.atomic
def deny_asset_request(request_instance, user, request=None, **kwargs):
    request_instance = AssetRequest.objects.for_scope(Scope.current()).select_for_update().get(pk=request_instance.pk)
    if request_instance.status != RequestStatusChoices.PENDING:
        raise ValidationError(_("Only pending requests can be denied."))

    request_instance.status = RequestStatusChoices.DENIED
    request_instance.responded_by = user
    request_instance.response_date = timezone.now()
    request_instance.response_notes = kwargs.get("response_notes", "")
    request_instance.save()

    if request_instance.is_group:
        for child in request_instance.sub_requests.filter(status=RequestStatusChoices.PENDING):
            child.status = RequestStatusChoices.DENIED
            child.responded_by = user
            child.response_date = timezone.now()
            child.response_notes = request_instance.response_notes
            child.save()

    return request_instance


# --- Asset Request Views ---


class RequestListView(ObjectListView):
    # select_related the FKs the table renderers touch (render_item reads
    # component/accessory/consumable; render_requested_for reads assigned_target =
    # assigned_user/assigned_location/assigned_asset) to avoid an N+1 per row.
    queryset = AssetRequest.objects.select_related(
        "requester",
        "asset_type",
        "asset",
        "responded_by",
        "component",
        "accessory",
        "consumable",
        "assigned_user",
        "assigned_location",
        "assigned_asset",
    )
    filterset = filters.AssetRequestFilterSet
    filterset_form = None
    table = tables.AssetRequestTable
    template_name = "assets/requests/assetrequest_list.html"
    action_buttons = ("add",)

    def has_permission(self):
        self._has_model_view_permission = super().has_permission()
        return self._has_model_view_permission or bool(_request_action_tenant_ids(self.request.user))

    def get_queryset(self):
        qs = super().get_queryset().filter(parent__isnull=True)
        user = self.request.user
        if user.is_staff and getattr(self, "_has_model_view_permission", False):
            return qs
        return qs.filter(Q(requester_id=user.pk) | Q(tenant_id__in=_request_action_tenant_ids(user)))

    def get_table(self):
        table = super().get_table()
        table.fulfillment_labels = request_fulfillment_labels([row.record for row in table.paginated_rows])
        return table

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        page_requests = [row.record for row in context["table"].paginated_rows]
        context["asset_request_bulk_receive_available"] = self._bulk_receive_available(page_requests)
        return context

    def _bulk_receive_available(self, page_requests):
        """Toolbar availability from the per-target decision only.

        An objectless ``has_perm("assets.fulfill_assetrequest")`` fails closed in
        the All-accessible scope, where the aggregate scope rejects ambient
        transaction permissions, even though the endpoint accepts the target.
        Approved groups stay selectable because the endpoint expands them into
        their approved request units.
        """
        candidates = [req for req in page_requests if req.status == RequestStatusChoices.APPROVED]
        group_ids = [req.pk for req in candidates if req.is_group]
        group_units = {}
        if group_ids:
            units = AssetRequest.objects.for_scope(Scope.current()).filter(
                parent_id__in=group_ids,
                status=RequestStatusChoices.APPROVED,
                is_group=False,
                deleted_at__isnull=True,
            )
            for unit in units:
                group_units.setdefault(unit.parent_id, []).append(unit)
        for req in candidates:
            if not can_asset_request_action(self.request.user, req, "bulk_receive"):
                continue
            if not req.is_group:
                if req.asset_id is None:
                    return True
                continue
            if any(
                can_asset_request_action(self.request.user, unit, "bulk_receive")
                for unit in group_units.get(req.pk, [])
            ):
                return True
        return False


class RequestDetailView(ObjectDetailView):
    queryset = AssetRequest.objects.select_related("requester", "asset_type", "asset", "responded_by")
    template_name = "assets/requests/assetrequest_detail.html"
    panels = [
        Panel("Asset Request Details", ["requester", "asset_type", "asset", "status", "request_date"]),
        Panel("Decision & Response Details", ["responded_by", "response_date", "response_notes"]),
        Panel("Requester Notes", ["notes"], position="right"),
    ]

    def has_permission(self):
        if super().has_permission():
            return True
        request_obj = self.get_object()
        return can_asset_request_action(self.request.user, request_obj, "approve") or can_asset_request_action(
            self.request.user, request_obj, "fulfill"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["claim_is_self_service"] = is_self_service_claim(self.request.user, self.object)
        if self.object.is_group:
            scope = Scope.current()
            children = list(
                AssetRequest.objects.for_scope(scope)
                .filter(parent=self.object)
                .select_related("requester", "asset_type", "asset", "responded_by")
            )
            visible_lines = PurchaseOrderLine.objects.for_scope(scope)
            links_by_request = {child.pk: [] for child in children}
            for link in FulfillmentLink.objects.filter(
                asset_request__in=children, purchase_order_line__in=visible_lines
            ).select_related("purchase_order_line__purchase_order"):
                links_by_request[link.asset_request_id].append(link)
            for child in children:
                child.visible_fulfillment_links = links_by_request[child.pk]
            context["asset_request_children"] = children
            context["asset_request_fulfillment_links"] = [
                link for child in children for link in child.visible_fulfillment_links
            ]
            labels = request_fulfillment_labels([self.object, *children])
        else:
            context["asset_request_children"] = []
            context["asset_request_fulfillment_links"] = list(
                self.object.fulfillment_links.select_related("purchase_order_line__purchase_order")
            )
            labels = request_fulfillment_labels([self.object])
        context["fulfillment_label"] = labels[self.object.pk]
        context["asset_request_fulfillment_labels"] = labels
        evidence = get_request_fulfillment_evidence(self.object)
        context["fulfillment_evidence"] = evidence
        context["fulfillment_method"] = evidence.get("method") if evidence else None
        return context

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff:
            return qs
        return qs.filter(Q(requester_id=user.pk) | Q(tenant_id__in=_request_action_tenant_ids(user)))


class RequestCreateView(ObjectEditView):
    queryset = AssetRequest.objects.all()
    model_form = AssetRequestForm
    template_name = "assets/requests/assetrequest_form.html"
    default_return_url = "assets:request_list"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def form_valid(self, form):
        form.instance.requester = self.request.user
        qty = form.cleaned_data.get("qty") or 1
        asset_type = form.instance.asset_type
        if asset_type and qty > 1:
            # Create the parent group request
            form.instance.qty = qty
            form.instance.is_group = True
            response = super().form_valid(form)

            # Create the individual child requests
            for _i in range(qty):
                req = AssetRequest(
                    tenant=form.instance.tenant,
                    requester=self.request.user,
                    asset_type=asset_type,
                    qty=1,
                    parent=form.instance,
                    assigned_user=form.instance.assigned_user,
                    assigned_location=form.instance.assigned_location,
                    assigned_asset=form.instance.assigned_asset,
                    notes=form.instance.notes,
                )
                req._skip_duplicate_check = True
                req.save()
            return response
        else:
            return super().form_valid(form)

    def alter_obj(self, obj, request, url_args, url_kwargs):
        obj.requester = request.user
        return obj


class RequestApproveView(GenericTransactionView):
    permission_required = "assets.approve_assetrequest"
    queryset = AssetRequest.objects.filter(status=RequestStatusChoices.PENDING)
    model_form = AssetRequestActionForm
    template_name = "assets/requests/assetrequest_approve.html"
    service_callable = approve_asset_request
    success_message = "Asset request approved successfully."

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request_instance"] = self.get_object()
        return kwargs

    def get_service_kwargs(self, form):
        return {
            "allocated_asset": form.cleaned_data.get("allocated_asset"),
            "allocated_location": form.cleaned_data.get("allocated_location"),
            "qty": form.cleaned_data.get("qty"),
            "response_notes": form.cleaned_data.get("response_notes"),
        }


class RequestDenyView(GenericTransactionView):
    permission_required = "assets.approve_assetrequest"
    queryset = AssetRequest.objects.filter(status=RequestStatusChoices.PENDING)
    model_form = AssetRequestActionForm
    template_name = "assets/requests/assetrequest_deny.html"
    service_callable = deny_asset_request
    success_message = "Asset request denied."

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request_instance"] = self.get_object()
        return kwargs

    def get_service_kwargs(self, form):
        return {"response_notes": form.cleaned_data.get("response_notes")}


class RequestCancelView(SimplePostView):
    # Self-authorizing: the requester may cancel their own request; everyone else
    # needs staff/approve_assetrequest. The per-object ownership check lives in
    # perform_action, so opt out of the static permission gate (fail-closed base).
    permission_required = ()
    queryset = AssetRequest.objects.all()

    def perform_action(self, obj, request):
        if not can_asset_request_action(request.user, obj, "cancel"):
            raise PermissionDenied(_("You do not have permission to cancel this request."))

        with transaction.atomic():
            # Deterministic global lock order: the unit's fulfilment links first, then the
            # request rows (the same relative order as the receipt path), so a concurrent
            # cancellation and receiving cannot deadlock.
            candidate_unit_ids = [obj.pk]
            if obj.is_group:
                candidate_unit_ids.extend(
                    obj.sub_requests.exclude(
                        status__in=[RequestStatusChoices.CANCELLED, RequestStatusChoices.FULFILLED]
                    ).values_list("pk", flat=True)
                )
            lock_unit_fulfillment_links(candidate_unit_ids)

            obj = AssetRequest.objects.for_scope(Scope.current()).select_for_update().get(pk=obj.pk)
            if obj.status not in [
                RequestStatusChoices.PENDING,
                RequestStatusChoices.APPROVED,
                RequestStatusChoices.PROCUREMENT,
            ]:
                raise ValidationError(_("Only pending, approved, or procurement requests can be cancelled."))

            if (
                obj.status == RequestStatusChoices.APPROVED
                and obj.asset
                and obj.asset.assignments.filter(is_active=True).exists()
            ):
                raise ValidationError(_("Cannot cancel a request that has already initiated active physical checkout."))

            obj.status = RequestStatusChoices.CANCELLED
            obj.save()

            cancelled_units = [obj]
            if obj.is_group:
                for child in (
                    obj.sub_requests.select_for_update()
                    .exclude(status__in=[RequestStatusChoices.CANCELLED, RequestStatusChoices.FULFILLED])
                    .order_by("request_date", "pk")
                ):
                    child.status = RequestStatusChoices.CANCELLED
                    child.save()
                    cancelled_units.append(child)

            # Unconsumed procurement links close with the cancelled units; received-quantity
            # attribution stays recorded on the soft-deleted rows and the PO line is untouched.
            release_fulfillment_links(cancelled_units)

        return {"message": _("Asset request cancelled successfully.")}


class RequestClaimView(SimplePostView):
    # Self-authorizing: the requester / assigned user may claim; everyone else
    # needs staff/fulfill_assetrequest. Ownership check is in perform_action, so
    # opt out of the static permission gate (fail-closed base).
    permission_required = ()
    queryset = AssetRequest.objects.all()

    def _claim_units(self, obj):
        if obj.is_group:
            all_children = list(
                AssetRequest.objects.for_scope(Scope.current())
                .select_for_update()
                .filter(
                    parent_id=obj.pk,
                    tenant_id=obj.tenant_id,
                    deleted_at__isnull=True,
                )
                .order_by("pk")
            )
            open_children = [
                child
                for child in all_children
                if child.status
                not in {
                    RequestStatusChoices.FULFILLED,
                    RequestStatusChoices.DENIED,
                    RequestStatusChoices.CANCELLED,
                }
            ]
            if any(child.status != RequestStatusChoices.APPROVED for child in open_children):
                raise ValidationError(_("Every open request unit must be approved before claiming."))
            requests_to_claim = [child for child in open_children if child.status == RequestStatusChoices.APPROVED]
            if not requests_to_claim:
                raise ValidationError(_("No open request unit is available to claim."))
        else:
            requests_to_claim = [obj]

        return requests_to_claim

    def _checkout_unit(self, req, request):
        holder = req.assigned_user
        location = req.assigned_location
        asset_target = req.assigned_asset

        if not holder and not location and not asset_target:
            holder = req.requester.asset_holder_profiles.filter(tenant=req.tenant).first()
            if not holder:
                raise ValidationError(
                    _("Requester does not have an active Asset Holder profile to assign the asset to.")
                )

        inventory_item = req.component or req.accessory or req.consumable
        with claim_fulfillment_scope(req.pk):
            if inventory_item:
                checkout_result = checkout_inventory_item(
                    inventory_item,
                    req.qty,
                    holder=holder,
                    location=location,
                    asset=asset_target,
                    source_location=req.source_location,
                    user=request.user,
                    request=request,
                    notes=_claim_handover_note(request.user, req),
                )
                transaction_ref = checkout_transaction_reference(
                    checkout_result,
                    quantity=req.qty,
                    expected_item=inventory_item,
                )
                completed_asset = None
            else:
                transaction_ref = self._checkout_asset_unit(req, request, holder, location, asset_target)
                completed_asset = req.asset

            complete_request_from_checkout(
                req,
                actor=request.user,
                transactions=[transaction_ref],
                request=request,
                asset=completed_asset,
            )

    def _checkout_asset_unit(self, req, request, holder, location, asset_target):
        existing_assignment_ids = set(
            AssetAssignment.objects.for_scope(Scope.current())
            .filter(
                asset_id=req.asset_id,
                is_active=True,
            )
            .values_list("pk", flat=True)
        )
        checkout_result = checkout_asset(
            asset=req.asset,
            holder=holder,
            location=location,
            asset_target=asset_target,
            user=request.user,
            request=request,
            notes=_claim_handover_note(request.user, req),
        )
        if checkout_result is None:
            raise ValidationError(_("Checkout did not return a recorded handover."))
        assignments = (
            AssetAssignment.objects.for_scope(Scope.current())
            .filter(
                asset_id=req.asset_id,
                is_active=True,
                checked_out_by_id=request.user.pk,
            )
            .exclude(pk__in=existing_assignment_ids)
        )
        if holder is not None:
            assignments = assignments.filter(assigned_user_id=holder.pk)
        if location is not None:
            assignments = assignments.filter(assigned_location_id=location.pk)
        if asset_target is not None:
            assignments = assignments.filter(assigned_asset_id=asset_target.pk)
        if assignments.count() != 1:
            raise ValidationError(_("Checkout did not return a unique recorded handover."))
        assignment = assignments.get()
        transaction_ref = checkout_transaction_reference(assignment)
        return transaction_ref

    def perform_action(self, obj, request):
        if not can_asset_request_action(request.user, obj, "claim"):
            raise PermissionDenied(_("You do not have permission to claim this asset."))

        with transaction.atomic():
            obj = (
                AssetRequest.objects.for_scope(Scope.current())
                .select_for_update()
                .get(
                    pk=obj.pk,
                    tenant_id=obj.tenant_id,
                    deleted_at__isnull=True,
                )
            )

            if obj.status != RequestStatusChoices.APPROVED:
                raise ValidationError(_("Only approved requests can be claimed."))

            requests_to_claim = self._claim_units(obj)

            # First validate all units before creating any checkout side effect.
            for req in requests_to_claim:
                is_inventory = req.component or req.accessory or req.consumable
                if not is_inventory and not req.asset:
                    raise ValidationError(_("No asset has been allocated to this request."))

            for req in requests_to_claim:
                self._checkout_unit(req, request)

            if (
                obj.is_group
                and not obj.sub_requests.filter(
                    deleted_at__isnull=True,
                )
                .exclude(
                    status__in=[
                        RequestStatusChoices.FULFILLED,
                        RequestStatusChoices.DENIED,
                        RequestStatusChoices.CANCELLED,
                    ]
                )
                .exists()
            ):
                obj.status = RequestStatusChoices.FULFILLED
                obj.response_date = timezone.now()
                obj.responded_by = request.user
                obj.response_notes = (
                    f"{(obj.response_notes or '').strip()}\nGroup fulfilled from request-unit handovers.".strip()
                )
                obj.save(update_fields=["status", "response_date", "responded_by", "response_notes"])

        count = len(requests_to_claim)
        return {
            "message": ngettext(
                "Claimed %(count)d item.",
                "Claimed %(count)d items.",
                count,
            )
            % {"count": count}
        }


class RequestMarkFulfilledView(GenericTransactionView):
    # Self-authorizing: requires staff/fulfill_assetrequest, checked in
    # perform_action. Opt out of the static permission gate (fail-closed base).
    permission_required = ()
    queryset = AssetRequest.objects.all()
    model_form = AssetRequestManualCompletionForm
    template_name = "assets/requests/assetrequest_mark_fulfilled.html"
    service_callable = manually_complete_request
    success_message = _("Request manually completed: no handover booked.")
    hx_redirect_on_success = True

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.pop("instance", None)
        return kwargs

    def get_service_kwargs(self, form):
        return {
            "reason": form.cleaned_data["reason"],
            "confirmed_no_handover": form.cleaned_data["confirmed_no_handover"],
        }

    def dispatch(self, request, *args, **kwargs):
        request_obj = self.get_object()
        if not can_asset_request_action(request.user, request_obj, "mark_fulfilled"):
            raise PermissionDenied(_("You do not have permission to mark this request as fulfilled."))
        return super().dispatch(request, *args, **kwargs)


class RequestBulkReceiveView(LoginRequiredMixin, View):
    # No ambient ``assets.fulfill_assetrequest`` guard: an objectless transaction
    # permission is rejected in the All-accessible scope, which made this endpoint
    # unreachable there. Authorization now rests on the per-target
    # ``bulk_receive`` decision (fail-closed) for every selected request and every
    # expanded child, matching the cancel/claim/mark-fulfilled views.
    def get(self, request, *args, **kwargs):
        return redirect("assets:request_list")

    def _require_bulk_receive_permission(self, requests):
        for req in requests:
            if not can_asset_request_action(self.request.user, req, "bulk_receive"):
                raise PermissionDenied(
                    _("You do not have permission to receive stock for one or more selected requests.")
                )

    @staticmethod
    def _validated_request_ids(request_ids):
        # Malformed or repeated selections are input errors, not authorization
        # failures: they surface as a visible message instead of a 403 page.
        try:
            ids = [int(request_id) for request_id in request_ids]
        except (TypeError, ValueError):
            raise ValidationError(_("One or more selected requests are invalid.")) from None
        if len(set(ids)) != len(ids):
            raise ValidationError(_("A request can appear only once in a bulk receipt."))
        return ids

    def _get_authorized_requests(self, request_ids, *, lock=False):
        ids = self._validated_request_ids(request_ids)
        queryset = AssetRequest.objects.filter(pk__in=ids, deleted_at__isnull=True)
        if lock:
            # No select_related() here: the nullable FKs would turn into outer joins,
            # and PostgreSQL rejects FOR UPDATE over the nullable side of an outer
            # join. Related rows are loaded on access inside the transaction.
            requests = list(queryset.select_for_update())
        else:
            requests = list(
                queryset.select_related(
                    "asset_type",
                    "requester",
                    "asset",
                    "assigned_location",
                    "source_location",
                )
            )
        if len(requests) != len(ids):
            raise PermissionDenied(_("You do not have access to one or more selected requests."))
        requests_by_id = {req.pk: req for req in requests}
        ordered_requests = [requests_by_id[request_id] for request_id in ids]
        self._require_bulk_receive_permission(ordered_requests)
        return ordered_requests

    def _validate_receipt(self, req, form):
        if req.status != RequestStatusChoices.APPROVED:
            raise ValidationError(_("Only approved requests can be received."))
        if req.is_group:
            raise ValidationError(_("Receive each group request unit separately; a group is not an allocatable asset."))
        if req.asset_id:
            raise ValidationError(_("This request already has an allocated asset."))
        location = form.cleaned_data["location"]
        supplier = form.cleaned_data["supplier"]
        if location and location.tenant_id != req.tenant_id:
            raise ValidationError(_("The selected location belongs to another tenant."))
        supplier_tenant_id = getattr(supplier, "tenant_id", None)
        if supplier_tenant_id is not None and supplier_tenant_id != req.tenant_id:
            raise ValidationError(_("The selected supplier belongs to another tenant."))

    def _get_requests_for_receipt(self, request_ids):
        selected_requests = self._get_authorized_requests(request_ids)
        approved_requests = [req for req in selected_requests if req.status == RequestStatusChoices.APPROVED]
        selected_ids = {req.pk for req in selected_requests}
        group_ids = [req.pk for req in approved_requests if req.is_group]
        child_requests = list(
            AssetRequest.objects.for_scope(Scope.current())
            .filter(parent_id__in=group_ids, deleted_at__isnull=True)
            .select_related(
                "asset_type",
                "requester",
                "asset",
                "assigned_location",
                "source_location",
            )
        )
        self._require_bulk_receive_permission(child_requests)
        return [req for req in approved_requests if not req.is_group] + [
            req
            for req in child_requests
            if req.status == RequestStatusChoices.APPROVED and not req.is_group and req.pk not in selected_ids
        ]

    def _render_formset(self, request, formset, requests_data):
        context = {
            "title": _("Bulk Stock Receipt & Allocation"),
            "formset": formset,
            "requests_data": requests_data,
        }
        return render(request, "assets/requests/bulk_receive.html", context)

    def _render_formset_errors(self, request, formset):
        requests_data = []
        for form in formset:
            try:
                request_id = int(form["request_id"].value())
                req = (
                    AssetRequest.objects.for_scope(Scope.current())
                    .filter(pk=request_id, deleted_at__isnull=True)
                    .first()
                )
            except (TypeError, ValueError):
                req = None
            requests_data.append((req, form))
        return self._render_formset(request, formset, requests_data)

    def _render_initial_formset(self, request, requests_qs):
        initial_data = []
        type_tag_seqs = {}
        for req in requests_qs:
            if not req.asset_type:
                continue
            dummy_asset = Asset(tenant=req.tenant, asset_type=req.asset_type)
            seq = AssetTagSequence.resolve_sequence_for_asset(dummy_asset)
            next_tag = ""
            if seq:
                if seq.pk not in type_tag_seqs:
                    type_tag_seqs[seq.pk] = (seq, seq.next_value)
                seq_obj, current_val = type_tag_seqs[seq.pk]
                next_tag = f"{seq_obj.prefix}{current_val:0{seq_obj.zero_padding}d}"
                type_tag_seqs[seq.pk] = (seq_obj, current_val + 1)

            deployable_status = StatusLabel.objects.filter(type="deployable").first()
            initial_data.append(
                {
                    "request_id": req.pk,
                    "asset_tag": next_tag,
                    "name": str(req.asset_type),
                    "status": deployable_status.pk if deployable_status else None,
                    "location": req.assigned_location.pk
                    if req.assigned_location
                    else (req.source_location.pk if req.source_location else None),
                }
            )

        formset = AssetReceiveFormSet(initial=initial_data)
        # strict=False: a request without an asset type intentionally produces no
        # formset row, so the two sequences may legitimately differ in length.
        return self._render_formset(request, formset, list(zip(requests_qs, formset, strict=False)))

    def _allocate_receipt_asset(self, req, form, user):
        asset = Asset.objects.create(
            name=form.cleaned_data["name"].strip(),
            asset_type=req.asset_type,
            asset_role=req.asset_type.asset_role if req.asset_type else None,
            serial_number=form.cleaned_data["serial_number"].strip() or "",
            asset_tag=form.cleaned_data["asset_tag"].strip() or "",
            status=form.cleaned_data["status"],
            location=form.cleaned_data["location"],
            supplier=form.cleaned_data["supplier"],
            order_number=form.cleaned_data["order_number"].strip() or "",
            purchase_cost=form.cleaned_data["purchase_cost"],
            purchase_date=form.cleaned_data["purchase_date"] or timezone.now().date(),
            tenant=req.tenant,
        )
        req.asset = asset
        req.status = RequestStatusChoices.APPROVED
        req.responded_by = user
        req.response_date = timezone.now()
        req.save()

    def _process_valid_formset(self, request, formset):
        try:
            with transaction.atomic():
                request_ids = [form.cleaned_data["request_id"] for form in formset]
                receipt_requests = self._get_authorized_requests(request_ids, lock=True)
                for req, form in zip(receipt_requests, formset, strict=True):
                    self._validate_receipt(req, form)
                for req, form in zip(receipt_requests, formset, strict=True):
                    self._allocate_receipt_asset(req, form, request.user)
                messages.success(request, _("Stock received and allocated; awaiting handover."))

            return redirect("assets:request_list")
        except PermissionDenied:
            raise
        # broad except: render-degrade: report a failed receipt batch instead of failing the page
        except Exception as error:
            messages.error(request, _("Error processing bulk receipt: %(error)s") % {"error": error})
            return self._render_formset_errors(request, formset)

    def post(self, request, *args, **kwargs):
        if "form-TOTAL_FORMS" in request.POST:
            formset = AssetReceiveFormSet(request.POST)
            if formset.is_valid():
                return self._process_valid_formset(request, formset)
            return self._render_formset_errors(request, formset)

        request_ids = request.POST.getlist("pk") or request.GET.getlist("pk")
        if not request_ids:
            messages.warning(request, _("No requests selected for bulk receipt."))
            return redirect("assets:request_list")

        try:
            requests_qs = self._get_requests_for_receipt(request_ids)
        except ValidationError as error:
            messages.error(request, "; ".join(str(message) for message in error.messages))
            return redirect("assets:request_list")
        if not requests_qs:
            messages.warning(request, _("None of the selected requests are in Approved status."))
            return redirect("assets:request_list")
        return self._render_initial_formset(request, requests_qs)

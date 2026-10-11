from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from assets.choices import RequestStatusChoices
from assets.models import Asset, AssetRequest, StatusLabel
from core.context import get_current_request_id
from core.tasks.context import TaskContext
from inventory.models import AccessoryStock, ComponentStock, ConsumableStock
from procurement.models import FulfillmentLink, PurchaseOrder, PurchaseOrderLine

_PURCHASE_ORDER_TRANSITIONS = {
    "approve": {PurchaseOrder.STATUS_DRAFT: frozenset({PurchaseOrder.STATUS_APPROVED})},
    "order": {PurchaseOrder.STATUS_APPROVED: frozenset({PurchaseOrder.STATUS_ORDERED})},
    "receive": {
        PurchaseOrder.STATUS_ORDERED: frozenset({PurchaseOrder.STATUS_PARTIAL, PurchaseOrder.STATUS_RECEIVED}),
        PurchaseOrder.STATUS_PARTIAL: frozenset({PurchaseOrder.STATUS_PARTIAL, PurchaseOrder.STATUS_RECEIVED}),
    },
    "cancel": {
        PurchaseOrder.STATUS_DRAFT: frozenset({PurchaseOrder.STATUS_CANCELLED}),
        PurchaseOrder.STATUS_APPROVED: frozenset({PurchaseOrder.STATUS_CANCELLED}),
        PurchaseOrder.STATUS_ORDERED: frozenset({PurchaseOrder.STATUS_CANCELLED}),
    },
    "reopen": {PurchaseOrder.STATUS_CANCELLED: frozenset({PurchaseOrder.STATUS_DRAFT})},
}

_PURCHASE_ORDER_TRANSITION_ERRORS = {
    "approve": _("Cannot approve a purchase order in '%(status)s' status."),
    "order": _("Cannot mark a purchase order as ordered when in '%(status)s' status. It must be Approved first."),
    "receive": _(
        "Cannot receive stock on a purchase order in '%(status)s' status. It must be Ordered or Partially Received."
    ),
    "cancel": _("Cannot cancel a purchase order in '%(status)s' status."),
    "reopen": _("Cannot reopen a purchase order in '%(status)s' status."),
}


def _lock_purchase_order(po):
    """Lock and re-read a live purchase order before inspecting or changing its state."""
    try:
        # unscoped: lock the live PO row regardless of ambient tenant-manager scope
        return PurchaseOrder._base_manager.select_for_update().get(pk=po.pk, deleted_at__isnull=True)
    except PurchaseOrder.DoesNotExist as exc:
        raise ValidationError(_("Purchase order no longer exists.")) from exc


def _validate_purchase_order_transition(po, action):
    """Reject a transition unless its locked source status appears in the single transition table."""
    if po.status not in _PURCHASE_ORDER_TRANSITIONS[action]:
        raise ValidationError(_PURCHASE_ORDER_TRANSITION_ERRORS[action] % {"status": po.get_status_display()})


def _validate_receipt_transition(po, submitted_status):
    """Preserve the stale-quantity error when another receipt already completed the PO."""
    if po.status == PurchaseOrder.STATUS_RECEIVED and submitted_status in {
        PurchaseOrder.STATUS_ORDERED,
        PurchaseOrder.STATUS_PARTIAL,
    }:
        return True
    _validate_purchase_order_transition(po, "receive")
    return False


def _transition_purchase_order(po, action, *, target_status=None, update_fields=()):
    """Persist a permitted transition using the shared source/target table."""
    _validate_purchase_order_transition(po, action)
    allowed_targets = _PURCHASE_ORDER_TRANSITIONS[action][po.status]
    if target_status is None:
        if len(allowed_targets) != 1:
            raise RuntimeError("A multi-target purchase-order transition requires an explicit target status.")
        target_status = next(iter(allowed_targets))
    if target_status not in allowed_targets:
        raise ValidationError(_PURCHASE_ORDER_TRANSITION_ERRORS[action] % {"status": po.get_status_display()})
    po.status = target_status
    po.save(update_fields=["status", *update_fields])
    return po


def _lock_purchase_order_for_asset_request(po):
    locked_po = _lock_purchase_order(po)
    if locked_po.tenant_id is None:
        raise ValidationError(_("Asset Request procurement requires a tenant-owned purchase order."))
    return locked_po


def _lock_asset_request_for_purchase_order(asset_request_id, po):
    try:
        asset_request_id = int(asset_request_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError(_("Invalid Asset Request identifier.")) from exc
    try:
        # unscoped: fulfillment lock by pk; the service authorizes tenant and state explicitly around it
        return AssetRequest._base_manager.select_for_update().get(
            pk=asset_request_id,
            tenant_id=po.tenant_id,
            deleted_at__isnull=True,
        )
    except AssetRequest.DoesNotExist as exc:
        raise ValidationError(_("Asset Request does not exist in the purchase order tenant.")) from exc


def _lock_fulfillment_targets(asset_request):
    if asset_request.parent_id is not None:
        raise ValidationError(_("Asset Request group children must be procured through their group parent."))
    if not asset_request.is_group:
        return [asset_request]
    # unscoped: fulfillment lock by pk; the service authorizes tenant and state explicitly around it
    targets = list(
        AssetRequest._base_manager.select_for_update()
        .filter(
            parent_id=asset_request.pk,
            tenant_id=asset_request.tenant_id,
            deleted_at__isnull=True,
        )
        .order_by("pk")
    )
    item_ids = (
        asset_request.asset_type_id,
        asset_request.component_id,
        asset_request.accessory_id,
        asset_request.consumable_id,
    )
    if (
        not targets
        or sum(target.qty for target in targets) != asset_request.qty
        or any(
            (
                target.asset_type_id,
                target.component_id,
                target.accessory_id,
                target.consumable_id,
            )
            != item_ids
            for target in targets
        )
    ):
        raise ValidationError(_("Asset Request group children do not match the approved parent request."))
    return targets


def _existing_fulfillment_link(asset_request, targets, po):
    target_ids = {target.pk for target in targets}
    candidate_ids = target_ids | {asset_request.pk}
    # unscoped: fulfillment lock by pk; the service authorizes tenant and state explicitly around it
    existing_links = list(
        FulfillmentLink._base_manager.select_related("purchase_order_line")
        .filter(asset_request_id__in=candidate_ids, deleted_at__isnull=True)
        .order_by("pk")
    )
    if not existing_links:
        return None
    if any(link.purchase_order_line.purchase_order_id != po.pk for link in existing_links):
        raise ValidationError(_("Asset Request is already linked to another purchase order."))
    linked_target_ids = {link.asset_request_id for link in existing_links}
    line_ids = {link.purchase_order_line_id for link in existing_links}
    if linked_target_ids != target_ids or len(line_ids) != 1:
        raise ValidationError(
            _("Asset Request fulfillment is incomplete. Review the linked requests before continuing.")
        )
    return existing_links[0]


def _create_fulfillment_link(po, asset_request, targets):
    line = PurchaseOrderLine(
        tenant=po.tenant,
        purchase_order=po,
        asset_type=asset_request.asset_type,
        component=asset_request.component,
        accessory=asset_request.accessory,
        consumable=asset_request.consumable,
        qty_ordered=asset_request.qty,
    )
    line.full_clean()
    line.save()
    links = []
    for target in targets:
        link = FulfillmentLink(
            tenant=po.tenant,
            asset_request=target,
            purchase_order_line=line,
            qty_allocated=target.qty,
            qty_received=0,
        )
        link.full_clean()
        link.save()
        target.status = RequestStatusChoices.PROCUREMENT
        target.save(update_fields=["status"])
        links.append(link)
    if asset_request.pk not in {target.pk for target in targets}:
        asset_request.status = RequestStatusChoices.PROCUREMENT
        asset_request.save(update_fields=["status"])
    return links[0]


@transaction.atomic
def link_asset_request_to_purchase_order(po, asset_request_id, user):
    """Create the Procurement-owned fulfillment graph for an approved Asset Request."""
    locked_po = _lock_purchase_order_for_asset_request(po)
    if user is None or not user.has_perm("procurement.change_purchaseorder", locked_po):
        raise PermissionDenied(_("You do not have permission to change this purchase order."))
    asset_request = _lock_asset_request_for_purchase_order(asset_request_id, locked_po)
    if not user.has_perm("assets.fulfill_assetrequest", asset_request):
        raise PermissionDenied(_("You do not have permission to fulfill this Asset Request."))
    targets = _lock_fulfillment_targets(asset_request)
    existing_link = _existing_fulfillment_link(asset_request, targets, locked_po)
    if existing_link is not None:
        return existing_link
    if any(target.asset_type_id is not None and target.qty > 1 for target in targets):
        raise ValidationError(
            _(
                "This request asks for more than one serialised unit. Split it into request "
                "units before linking it to a purchase order."
            )
        )
    if locked_po.status != PurchaseOrder.STATUS_DRAFT:
        raise ValidationError(_("Asset Requests can be linked only to draft purchase orders."))
    if asset_request.status != RequestStatusChoices.APPROVED or any(
        target.status != RequestStatusChoices.APPROVED for target in targets
    ):
        raise ValidationError(_("Only approved Asset Requests can be linked to a purchase order."))

    return _create_fulfillment_link(locked_po, asset_request, targets)


def lock_unit_fulfillment_links(asset_request_ids):
    """Lock the live fulfilment links of the given request units in deterministic pk order.

    Must be called inside an open transaction. Cancellation calls this BEFORE locking the
    request rows so every transaction acquires these two lock classes in the same global
    order as the receipt path (links, then requests); a concurrent cancellation and
    receiving can then never deadlock.
    """
    request_ids = sorted({int(asset_request_id) for asset_request_id in asset_request_ids})
    if not request_ids:
        return []
    # unscoped: fulfillment lock by pk; the service authorizes tenant and state explicitly around it
    return list(
        FulfillmentLink._base_manager.select_for_update()
        .filter(asset_request_id__in=request_ids, deleted_at__isnull=True)
        .order_by("pk")
    )


@transaction.atomic
def release_fulfillment_links(asset_requests):
    """Close the fulfillment links of cancelled request units.

    Received-quantity attribution stays recorded on the (soft-deleted) link rows, so the
    delivered share of a cancelled request remains reconstructible, and the purchase order
    line is left untouched for an explicit operator decision.
    """
    request_ids = [asset_request.pk for asset_request in asset_requests]
    if not request_ids:
        return 0
    links = lock_unit_fulfillment_links(request_ids)
    # Lock the request rows after their links (global order: links, then requests) so the
    # relative acquisition order matches the receipt path.
    # unscoped: fulfillment lock by pk; the service authorizes tenant and state explicitly around it
    list(
        AssetRequest._base_manager.select_for_update()
        .filter(pk__in=set(request_ids), deleted_at__isnull=True)
        .order_by("request_date", "pk")
    )
    for link in links:
        link.delete()
    return len(links)


def _lock_receipt_stock_rows(lines, line_quantities, location):
    """Create and lock every stock row in one deterministic global order."""
    stock_maps = {}
    specifications = (
        (ComponentStock, "component_id"),
        (AccessoryStock, "accessory_id"),
        (ConsumableStock, "consumable_id"),
    )
    for stock_model, item_field in specifications:
        item_ids = sorted(
            {
                getattr(line, item_field)
                for line in lines
                if getattr(line, item_field) is not None and line_quantities.get(line.pk, 0) > 0
            }
        )
        for item_id in item_ids:
            stock_model.objects.get_or_create(
                **{item_field: item_id, "location": location},
                defaults={"qty": 0},
            )
        locked = (
            stock_model.objects.select_for_update()
            .filter(**{f"{item_field}__in": item_ids, "location": location})
            .order_by(item_field)
        )
        stock_maps[stock_model] = {getattr(stock, item_field): stock for stock in locked}
    return stock_maps


def _lock_line_fulfillment_links(line):
    """Lock the line's live links and their requests in deterministic (request_date, pk) order."""
    # unscoped: fulfillment lock by pk; the service authorizes tenant and state explicitly around it
    links = list(
        FulfillmentLink._base_manager.select_for_update()
        .filter(purchase_order_line=line, deleted_at__isnull=True)
        .order_by("pk")
    )
    if not links:
        return []
    # unscoped: fulfillment lock by pk; the service authorizes tenant and state explicitly around it
    requests = {
        request.pk: request
        for request in AssetRequest._base_manager.select_for_update()
        .filter(pk__in={link.asset_request_id for link in links}, deleted_at__isnull=True)
        .order_by("request_date", "pk")
    }
    pairs = [(requests[link.asset_request_id], link) for link in links if link.asset_request_id in requests]
    pairs.sort(key=lambda pair: (pair[0].request_date, pair[0].pk, pair[1].pk))
    return pairs


def _receivable_fulfillment_pairs(line):
    """Live links whose request still awaits procurement, holds no asset, and has an outstanding quantity."""
    return [
        (request, link)
        for request, link in _lock_line_fulfillment_links(line)
        if request.status == RequestStatusChoices.PROCUREMENT and request.asset_id is None and link.qty_outstanding > 0
    ]


def _attribute_quantity_receipt(line, qty):
    """Attribute a received quantity across the line's outstanding requests.

    Only a request whose full pledged quantity has been received becomes approved; the
    remaining quantity stays outstanding and every surplus unit remains free stock.
    """
    remaining = qty
    touched = []
    for request, link in _receivable_fulfillment_pairs(line):
        if remaining <= 0:
            break
        attributed = min(link.qty_outstanding, remaining)
        link.qty_received = (link.qty_received or 0) + attributed
        link.save(update_fields=["qty_received"])
        remaining -= attributed
        if link.qty_outstanding == 0:
            request.status = RequestStatusChoices.APPROVED
            request.save(update_fields=["status"])
        touched.append(request)
    _approve_completed_group_parents(touched)


def _approve_completed_group_parents(linked_requests):
    parent_ids = sorted({request.parent_id for request in linked_requests if request.parent_id is not None})
    if not parent_ids:
        return
    # unscoped: fulfillment lock by pk; the service authorizes tenant and state explicitly around it
    parents = AssetRequest._base_manager.select_for_update().filter(
        pk__in=parent_ids,
        status=RequestStatusChoices.PROCUREMENT,
        deleted_at__isnull=True,
    )
    for parent in parents:
        # unscoped: fulfillment lock by pk; the service authorizes tenant and state explicitly around it
        has_pending_child = AssetRequest._base_manager.filter(
            parent_id=parent.pk,
            status=RequestStatusChoices.PROCUREMENT,
            deleted_at__isnull=True,
        ).exists()
        if not has_pending_child:
            parent.status = RequestStatusChoices.APPROVED
            parent.save(update_fields=["status"])


def _receive_asset_line(line, qty, details, po, deployable_status):
    """Materialise received serialised units and allocate them to outstanding request units."""
    # Linked requests still awaiting procurement, in deterministic allocation order. A
    # serialised request unit receives exactly one asset; it becomes approved only once
    # its full pledged quantity has been received (multi-unit pledges are not linkable).
    pairs = _receivable_fulfillment_pairs(line)
    pair_idx = 0
    for i in range(qty):
        detail = details[i] if i < len(details) else {}
        asset_name = detail.get("name") or str(line.asset_type)
        if not asset_name:
            asset_name = f"{line.asset_type.manufacturer.name} {line.asset_type.model}"
        asset = Asset.objects.create(
            name=asset_name.strip(),
            asset_type=line.asset_type,
            serial_number=detail.get("serial_number", "").strip() or "",
            asset_tag=detail.get("asset_tag", "").strip() or "",  # If empty, save() auto-generates
            status=deployable_status,
            location=po.destination_location,
            supplier=po.supplier,
            purchase_cost=line.unit_price,
            currency=po.currency,
            purchase_date=timezone.now().date(),
            order_number=po.order_number,
            tenant=po.tenant,
            purchase_order_line=line,
        )
        # Allocate this asset to the next request unit that still awaits procurement
        if pair_idx < len(pairs):
            req, link = pairs[pair_idx]
            req.asset = asset
            received = (link.qty_received or 0) + 1
            # Approve only once the link's full pledged quantity has been received; a legacy
            # multi-unit pledge keeps its first asset without fabricating a full delivery.
            if received >= link.qty_allocated:
                req.status = RequestStatusChoices.APPROVED
            req.save()
            link.qty_received = received
            link.save(update_fields=["qty_received"])
            pair_idx += 1
    _approve_completed_group_parents([req for req, _ in pairs[:pair_idx]])


def _group_details_by_line(asset_details):
    """Group received-asset details by their purchase order line."""
    details_by_line = {}
    for detail in asset_details or []:
        if not detail or "line_id" not in detail:
            continue
        details_by_line.setdefault(int(detail["line_id"]), []).append(detail)
    return details_by_line


def _receive_line(line, qty, details_by_line, po, deployable_status, stock_maps):
    """Apply one line's receipt: guard the balance, dispatch by item type, record the line total."""
    if qty <= 0:
        return
    if qty > line.qty_outstanding:
        raise ValidationError(
            _("Cannot receive %(qty)s for line %(line)s because only %(outstanding)s remain outstanding.")
            % {"qty": qty, "line": line.pk, "outstanding": line.qty_outstanding}
        )
    if line.asset_type:
        _receive_asset_line(line, qty, details_by_line.get(line.pk, []), po, deployable_status)
    else:
        _receive_stock_line(line, qty, stock_maps)
    line.qty_received += qty
    line.save(update_fields=["qty_received"])


def _receive_stock_line(line, qty, stock_maps):
    """Grow the locked stock row of a component, accessory, or consumable line."""
    if line.component_id is not None:
        stock = stock_maps[ComponentStock][line.component_id]
    elif line.accessory_id is not None:
        stock = stock_maps[AccessoryStock][line.accessory_id]
    elif line.consumable_id is not None:
        stock = stock_maps[ConsumableStock][line.consumable_id]
    else:
        return
    stock.qty += qty
    stock.save()
    _attribute_quantity_receipt(line, qty)


def _assert_receipt_state(lines, line_quantities, expected_received):
    """Refuse stale receipt submissions before any mutation happens.

    Called with the purchase order lines already locked, so a concurrent receipt that
    committed first is visible here and its replay is refused deterministically.
    """
    for line in lines:
        if line_quantities.get(line.pk, 0) <= 0:
            continue
        if line.pk not in expected_received:
            raise ValidationError(
                _(
                    "The receipt submission does not state the recorded quantity it was prepared against for line "
                    "%(line)s. Reload the receive form and submit again."
                )
                % {"line": line.pk}
            )
        expected = expected_received[line.pk]
        if line.qty_received != expected:
            raise ValidationError(
                _(
                    "The recorded quantity for line %(line)s changed since this receipt was prepared "
                    "(expected %(expected)s, found %(found)s). Reload the receive form and submit again."
                )
                % {"line": line.pk, "expected": expected, "found": line.qty_received}
            )


@transaction.atomic
def receive_purchase_order(po, line_quantities, asset_details=None, *, expected_received):
    """
    line_quantities: dict of {line_id (int): qty_to_receive (int)}
    asset_details: list of dicts [{'line_id': int, 'serial_number': str, 'asset_tag': str, 'name': str}]
    expected_received: dict of {line_id (int): qty_received (int)} this submission was prepared against.

    Every submitted delivery must state the recorded receipt quantity it saw (the documented
    retry contract): replays, parallel duplicates, and obsolete forms whose lines have moved
    on are refused before anything mutates, so a repeated submission can never silently book
    additional stock. Each accepted submission is a new partial delivery.
    """
    submitted_status = po.status
    po = _lock_purchase_order(po)
    completed_concurrently = _validate_receipt_transition(po, submitted_status)

    any_outstanding = False

    lines = list(po.lines.select_for_update().order_by("pk"))
    _assert_receipt_state(lines, line_quantities, expected_received)
    if completed_concurrently:
        _validate_purchase_order_transition(po, "receive")

    # Pre-fetch deployable status label only after stale submissions are refused.
    deployable_status = StatusLabel.objects.filter(type="deployable").first()
    if not deployable_status:
        raise ValidationError(_("Deployable status label does not exist in the database."))

    details_by_line = _group_details_by_line(asset_details)
    stock_maps = _lock_receipt_stock_rows(lines, line_quantities, po.destination_location)

    for line in lines:
        qty = line_quantities.get(line.pk, 0)
        _receive_line(line, qty, details_by_line, po, deployable_status, stock_maps)
        if line.qty_outstanding > 0:
            any_outstanding = True

    target_status = PurchaseOrder.STATUS_PARTIAL if any_outstanding else PurchaseOrder.STATUS_RECEIVED
    _transition_purchase_order(po, "receive", target_status=target_status)


@transaction.atomic
def approve_purchase_order(po, user=None, request=None):
    """Transition PO from draft to approved status."""
    po = _lock_purchase_order(po)
    _validate_purchase_order_transition(po, "approve")
    if not po.lines.exists():
        raise ValidationError(_("Cannot approve a purchase order with no line items."))
    # Segregation of duties: the user who created the PO must not approve it.
    if user is not None and po.created_by_id and po.created_by_id == getattr(user, "id", None):
        raise ValidationError(_("A purchase order cannot be approved by the user who created it."))
    _transition_purchase_order(po, "approve")
    return {"message": _("Purchase Order %(number)s has been approved.") % {"number": po.order_number}}


@transaction.atomic
def order_purchase_order(po, user=None, request=None):
    """Transition PO from approved to ordered status."""
    po = _lock_purchase_order(po)
    _validate_purchase_order_transition(po, "order")
    if not po.order_date:
        po.order_date = timezone.now().date()
    _transition_purchase_order(po, "order", update_fields=("order_date",))
    return {"message": _("Purchase Order %(number)s marked as Ordered.") % {"number": po.order_number}}


@transaction.atomic
def cancel_purchase_order(po, user=None, request=None):
    """Transition the PO, supplying system audit attribution for non-request callers."""
    if get_current_request_id() is not None:
        return _cancel_purchase_order(po)
    with TaskContext(operation="procurement.purchase_order.cancel"):
        return _cancel_purchase_order(po)


def _cancel_purchase_order(po):
    """Transition PO from draft, approved, or ordered to cancelled status."""
    po = _lock_purchase_order(po)
    _validate_purchase_order_transition(po, "cancel")

    # Lock every live PO line before fulfilment links and their request rows.
    lines = list(po.lines.select_for_update().order_by("pk"))
    line_ids = {line.pk for line in lines}
    if line_ids:
        # unscoped: soft-deleted links identify request-derived lines across cancellation retries
        historical_links = list(
            FulfillmentLink._base_manager.filter(purchase_order_line_id__in=line_ids).values_list(
                "asset_request_id", "purchase_order_line_id"
            )
        )
    else:
        historical_links = []
    request_ids = {request_id for request_id, _line_id in historical_links}
    # Preserve the shared lock order: PO -> lines -> links -> requests.
    locked_links = lock_unit_fulfillment_links(request_ids)
    links = [link for link in locked_links if link.purchase_order_line_id in line_ids]
    # unscoped: locked PO link IDs remain authoritative outside ambient tenant scope
    requests = {
        request.pk: request
        for request in AssetRequest._base_manager.select_for_update()
        .filter(pk__in={link.asset_request_id for link in links}, deleted_at__isnull=True)
        .order_by("request_date", "pk")
    }

    reverted_requests = []
    for link in links:
        req = requests.get(link.asset_request_id)
        if req is not None and req.status == RequestStatusChoices.PROCUREMENT:
            req.status = RequestStatusChoices.APPROVED
            req.save(update_fields=["status"])
            reverted_requests.append(req)
    request_line_ids = {link.purchase_order_line_id for link in links}
    # SoftDeleteMixin never cascades (#619): retire the links of each removed line explicitly.
    for link in links:
        link.delete()
    for line in lines:
        if line.pk in request_line_ids:
            line.delete()
    _approve_completed_group_parents(reverted_requests)

    _transition_purchase_order(po, "cancel")
    return {
        "message": _("Purchase Order %(number)s cancelled. Linked asset requests reverted to Approved status.")
        % {"number": po.order_number}
    }


@transaction.atomic
def reopen_purchase_order(po, user=None, request=None):
    """Transition PO from cancelled back to draft status."""
    po = _lock_purchase_order(po)
    _validate_purchase_order_transition(po, "reopen")
    _transition_purchase_order(po, "reopen")
    return {"message": _("Purchase Order %(number)s has been reopened and set to Draft.") % {"number": po.order_number}}

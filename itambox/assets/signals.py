import logging

from django.db import DatabaseError, models, transaction
from django.db.models import signals as model_signals
from django.db.models.signals import post_save
from django.dispatch import receiver
from django_q.tasks import async_task

from assets.choices import RequestStatusChoices
from assets.models import AssetAssignment, AssetRequest, StatusLabel
from assets.services.request_fulfillment import (
    checkout_transaction_reference,
    claim_target_request_id,
    complete_request_from_checkout,
)
from extras.services.events import dispatch_event
from organization.models import AssetHolder

logger = logging.getLogger(__name__)


@receiver(model_signals.post_migrate, dispatch_uid="assets.ensure_canonical_missing_status")
def ensure_canonical_missing_status(sender, using, **kwargs):
    """Restore required reference data after migrate and test-database flushes."""
    if sender.label != "assets":
        return
    StatusLabel._base_manager.using(using).get_or_create(
        slug="missing",
        defaults={"name": "Missing", "type": StatusLabel.TYPE_UNDEPLOYABLE, "color": "dc3545"},
    )


@receiver(post_save, sender=AssetAssignment)
def on_asset_assignment_save(sender, instance, created, **kwargs):
    try:
        if created:
            transaction.on_commit(lambda: dispatch_event(sender, instance, action="checkout"))
        elif not instance.is_active and instance.checked_in_at:
            transaction.on_commit(lambda: dispatch_event(sender, instance, action="checkin"))
    except DatabaseError as e:
        logger.exception("Database error occurred while processing asset assignment event: %s", e)
    except Exception as e:
        logger.exception("Unexpected error occurred while processing asset assignment event: %s", e)


@receiver(post_save, sender=AssetRequest)
def on_asset_request_save(sender, instance, created, **kwargs):
    try:
        if created:
            transaction.on_commit(lambda: dispatch_event(sender, instance, action="create"))

            # Only notify admins for parent requests or standalone requests, avoiding N+1 queries
            if instance.parent is None:
                request_id = instance.pk
                transaction.on_commit(lambda: async_task("assets.tasks.requests.notify_new_request_task", request_id))
    except DatabaseError as e:
        logger.exception("Database error occurred while processing asset request notification: %s", e)
    except Exception as e:
        logger.exception("Unexpected error occurred while processing asset request notification: %s", e)


def _pick_fulfillment_unit(units, asset_pk, assignee_pk, assignee_user_id):
    """Choose the single request unit an asset checkout may satisfy.

    A request qualifies when it explicitly targets the receiving holder, or
    when it has no target at all and the receiving holder belongs to the
    requester. An exact asset match wins over an asset-type match; among
    equals the caller's ordering (request_date, pk) decides. Requests
    targeting a different holder/location/asset never qualify.
    """
    eligible = []
    for req in units:
        if req.assigned_user_id == assignee_pk:
            eligible.append(req)
        elif (
            req.assigned_user_id is None
            and req.assigned_location_id is None
            and req.assigned_asset_id is None
            and req.requester_id == assignee_user_id
        ):
            eligible.append(req)
    for req in eligible:
        if req.asset_id == asset_pk:
            return req
    return eligible[0] if eligible else None


def _mark_request_fulfilled(req, instance, asset=None):
    complete_request_from_checkout(
        req,
        actor=instance.checked_out_by,
        transactions=[checkout_transaction_reference(instance)],
        asset=asset,
    )


def _append_response_note(existing, note):
    existing = (existing or "").strip()
    return f"{existing}\n{note}".strip() if existing else note


@receiver(post_save, sender=AssetAssignment)
def auto_fulfill_asset_requests(sender, instance, created, **kwargs):
    """
    One new active assignment fulfils exactly one open request unit.

    Group children are fulfilled individually and the parent only flips to
    fulfilled once no open unit remains. Procurement requests and requests
    targeting a different holder/location/asset are never touched here.
    """
    if created and instance.is_active:
        with transaction.atomic():
            asset = instance.asset
            assignee = instance.assigned_user
            if not isinstance(assignee, AssetHolder):
                return

            units = (
                AssetRequest.objects.filter(
                    status__in=[RequestStatusChoices.PENDING, RequestStatusChoices.APPROVED],
                    tenant=asset.tenant,
                )
                .filter(models.Q(asset=asset) | models.Q(asset_type=asset.asset_type, asset__isnull=True))
                .exclude(is_group=True)
                .order_by("request_date", "pk")
            )
            intended_request_pk = claim_target_request_id()
            if intended_request_pk is not None:
                units = units.filter(pk=intended_request_pk)

            unit = _pick_fulfillment_unit(units, asset.pk, assignee.pk, assignee.user_id)
            if unit is None:
                return

            _mark_request_fulfilled(unit, instance, asset=asset)

            if unit.parent_id:
                parent = AssetRequest._base_manager.select_for_update().get(
                    pk=unit.parent_id,
                    tenant_id=asset.tenant_id,
                    deleted_at__isnull=True,
                )
                open_units_exist = (
                    parent.sub_requests.filter(
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
                )
                if not open_units_exist and parent.status == RequestStatusChoices.APPROVED:
                    parent.status = RequestStatusChoices.FULFILLED
                    parent.responded_by = instance.checked_out_by
                    parent.response_date = instance.checked_out_at
                    parent.response_notes = _append_response_note(
                        parent.response_notes,
                        f"Child request unit fulfilled via assignment #{instance.pk}; no group handover booked.",
                    )
                    parent.save(update_fields=["status", "responded_by", "response_date", "response_notes"])

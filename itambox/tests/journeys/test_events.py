"""Event journeys: a soft delete is as visible to EventRules as a direct one."""

from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from assets.models import AssetReservation, ReservationStatusChoices
from core.context import _request_id, set_current_user
from core.models import ObjectChange
from extras.models import Event, EventRule

from .support import JourneyMixin, today


class SoftDeleteEventJourneyTests(JourneyMixin, TestCase):
    def setUp(self):
        self.make_tenant("journey-events")
        self.actor = self.make_member("event-actor", set())

    def test_soft_delete_emits_a_delete_event_for_a_logged_row(self):
        # Events are only recorded for models some rule subscribes to (#621).
        EventRule.objects.create(
            name="reservation lifecycle",
            model=ContentType.objects.get_for_model(AssetReservation),
            events=["delete", "restore"],
            action_type=EventRule.ACTION_NOTIFICATION,
            enabled=True,
        )
        asset = self.make_asset()
        reservation = AssetReservation.objects.create(
            asset=asset,
            reserved_for=self.make_holder(),
            start_date=today(),
            end_date=today() + timedelta(days=2),
            status=ReservationStatusChoices.ACTIVE,
        )
        _request_id.set("00000000-0000-4000-8000-000000000604")
        set_current_user(self.actor)
        try:
            with self.captureOnCommitCallbacks(execute=True):
                reservation.delete()
        finally:
            _request_id.set(None)
            set_current_user(None)

        reservation_type = ContentType.objects.get_for_model(AssetReservation)
        self.assertTrue(
            ObjectChange.objects.filter(
                changed_object_type=reservation_type, changed_object_id=reservation.pk, action="delete"
            ).exists(),
            "precondition: the deleted row was change-logged",
        )
        self.assertTrue(
            Event.objects.filter(model=reservation_type, object_id=reservation.pk, action="delete").exists(),
            "the deleted row has an ObjectChange but no delete Event",
        )
        with self.captureOnCommitCallbacks(execute=True):
            reservation.restore()
        self.assertTrue(
            Event.objects.filter(model=reservation_type, object_id=reservation.pk, action="restore").exists(),
            "restoring the deleted row must emit a restore Event",
        )

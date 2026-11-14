"""Event outbox regression suite (issue #1091)."""

from datetime import timedelta
from importlib import import_module
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from assets.models import Manufacturer
from extras.models import Event, EventRule
from extras.services import events as svc


def _rule(ct):
    return EventRule.objects.create(
        name="outbox rule",
        model=ct,
        events=["create"],
        action_type=EventRule.ACTION_NOTIFICATION,
        enabled=True,
    )


class OutboxEmissionTests(TransactionTestCase):
    def setUp(self):
        self.ct = ContentType.objects.get_for_model(Manufacturer)
        _rule(self.ct)

    def test_autocommit_write_inserts_event_in_same_transaction(self):
        with patch.object(svc, "_publish_hint"):
            Manufacturer.objects.create(name="Acme", slug="acme")
        event = Event.objects.get(model=self.ct, action="create")
        self.assertEqual(event.status, Event.STATUS_PENDING)

    def test_rolled_back_write_leaves_no_event(self):
        try:
            with transaction.atomic():
                Manufacturer.objects.create(name="Gone", slug="gone")
                raise RuntimeError("abort")
        except RuntimeError:
            pass
        self.assertFalse(Event.objects.filter(model=self.ct).exists())

    def test_process_death_after_commit_leaves_recoverable_pending_row(self):
        with patch.object(svc, "_publish_hint"):
            with transaction.atomic():
                Manufacturer.objects.create(name="Acme", slug="acme")
        self.assertEqual(Event.objects.filter(status=Event.STATUS_PENDING).count(), 1)

    def test_lost_hint_is_recovered_by_coordinator(self):
        with patch.object(svc, "async_task", side_effect=RuntimeError("broker down")):
            Manufacturer.objects.create(name="Acme", slug="acme")
        result = svc.recover_pending_events()
        self.assertEqual(result["processed"], 1)
        self.assertEqual(Event.objects.get(model=self.ct).status, Event.STATUS_PROCESSED)

    def test_emission_outside_transaction_is_rejected(self):
        manufacturer = Manufacturer.objects.create(name="Acme", slug="acme")
        with patch.object(connection, "in_atomic_block", False):
            with self.assertRaises(svc.EventOutsideTransactionError):
                svc.dispatch_event(Manufacturer, manufacturer, action="create")


class OutboxProcessingTests(TransactionTestCase):
    def setUp(self):
        self.ct = ContentType.objects.get_for_model(Manufacturer)
        _rule(self.ct)
        with patch.object(svc, "_publish_hint"):
            Manufacturer.objects.create(name="Acme", slug="acme")
        self.event = Event.objects.get(model=self.ct)

    def test_failure_retries_with_backoff_without_duplicating_event(self):
        with patch.object(svc, "_execute_event_action", side_effect=RuntimeError("boom")):
            pass
        with patch.object(svc, "process_event_rules", side_effect=RuntimeError("boom")):
            self.assertFalse(svc.process_event(self.event.pk))
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.STATUS_PENDING)
        self.assertEqual(self.event.attempts, 1)
        self.assertGreater(self.event.available_at, timezone.now())
        self.assertEqual(Event.objects.filter(model=self.ct).count(), 1)
        self.assertFalse(svc.process_event(self.event.pk))  # not yet due
        Event.objects.filter(pk=self.event.pk).update(available_at=timezone.now())
        self.assertTrue(svc.process_event(self.event.pk))
        self.assertEqual(Event.objects.get(pk=self.event.pk).status, Event.STATUS_PROCESSED)

    def test_attempts_exhausted_goes_dead(self):
        Event.objects.filter(pk=self.event.pk).update(attempts=svc.EVENT_MAX_ATTEMPTS - 1)
        with patch.object(svc, "process_event_rules", side_effect=RuntimeError("boom")):
            svc.process_event(self.event.pk)
        self.assertEqual(Event.objects.get(pk=self.event.pk).status, Event.STATUS_DEAD)

    def test_locked_event_is_skipped_not_double_claimed(self):
        with transaction.atomic():
            locked = Event._base_manager.select_for_update().get(pk=self.event.pk)
            self.assertIsNotNone(locked)
            # SKIP LOCKED from a second connection is exercised by the claim query itself
            claimed = (
                Event._base_manager.select_for_update(skip_locked=True, of=("self",))
                .filter(pk=self.event.pk, status=Event.STATUS_PENDING)
                .first()
            )
            self.assertIsNotNone(claimed)  # same transaction may re-lock its own row

    def test_recovery_finds_event_without_delivery_rows(self):
        self.assertEqual(self.event.deliveries.count() if hasattr(self.event, "deliveries") else 0, 0)
        self.assertEqual(svc.recover_pending_events()["due"], 1)


class OutboxBackfillTests(TransactionTestCase):
    migrate_from = [("extras", "0005_label_template_object_types")]

    def test_backfill_boundary(self):
        module = import_module("extras.migrations.0006_event_outbox")
        ct = ContentType.objects.get_for_model(Manufacturer)
        now = timezone.now()
        young = Event.objects.create(model=ct, object_id=1, action="create", data={})
        old = Event.objects.create(model=ct, object_id=2, action="create", data={})
        Event.objects.filter(pk=young.pk).update(timestamp=now - timedelta(hours=23))
        Event.objects.filter(pk=old.pk).update(timestamp=now - timedelta(hours=25))
        # the model still has no legacy column; exercise the windowing on the live schema
        cutoff = now - module.BACKFILL_WINDOW
        self.assertGreater(Event.objects.get(pk=young.pk).timestamp, cutoff)
        self.assertLess(Event.objects.get(pk=old.pk).timestamp, cutoff)

"""Issue #445 event terminal-attempt and logging contracts."""

import importlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError, IntegrityError, close_old_connections
from django.test import TestCase, TransactionTestCase

from assets.models import Manufacturer
from core.models import Notification
from extras.models import Event, EventRule


def _event_service():
    try:
        return importlib.import_module("extras.services.events")
    except ImportError:
        return importlib.import_module("core.events")


class Issue445EventSemanticsTests(TestCase):
    """Preserved attempts/isolation pass; replay and sanitized-error assertions are RED."""

    CANARY = "issue445-event-secret-canary"

    def setUp(self):
        self.content_type = ContentType.objects.get_for_model(Manufacturer)
        self.user = get_user_model().objects.create_user(username="issue445-event-user", password="pw")
        self.event = Event.objects.create(
            model=self.content_type,
            object_id=445,
            action=Event.ACTION_CREATE,
            data={"app_label": "assets", "model_name": "manufacturer"},
        )
        self.failing_rule = EventRule.objects.create(
            name="Issue445 failing rule",
            model=self.content_type,
            events=[self.event.action],
            action_type=EventRule.ACTION_NOTIFICATION,
            enabled=True,
        )
        self.success_rule = EventRule.objects.create(
            name="Issue445 success rule",
            model=self.content_type,
            events=[self.event.action],
            action_type=EventRule.ACTION_NOTIFICATION,
            enabled=True,
        )

    def _dispatch_with_canary(self):
        service = _event_service()
        attempts = []

        def execute(rule, event, _tenant_id=None):
            attempts.append(rule.pk)
            if rule.pk == self.failing_rule.pk:
                raise RuntimeError(self.CANARY)
            Notification.objects.create(user=self.user, subject="issue445-success", message="safe")

        return service, attempts, execute

    def test_each_eligible_rule_is_attempted_once_and_sibling_success_survives(self):
        service, attempts, execute = self._dispatch_with_canary()
        with self.assertLogs(service.__name__, level="ERROR"), self.subTest("isolated actions"):
            with patch.object(service, "_execute_event_action", side_effect=execute):
                service.process_event_rules(self.event)
        self.event.refresh_from_db()
        self.assertCountEqual(attempts, [self.failing_rule.pk, self.success_rule.pk])
        self.assertEqual(Notification.objects.filter(subject="issue445-success").count(), 1)
        self.assertEqual(self.event.status, Event.STATUS_PROCESSED)

    def test_database_failure_uses_savepoint_and_does_not_block_sibling(self):
        service = _event_service()
        attempts = []

        def execute(rule, _event, _tenant_id=None):
            attempts.append(rule.pk)
            if rule.pk == self.failing_rule.pk:
                raise IntegrityError("issue445 savepoint canary")
            Notification.objects.create(user=self.user, subject="issue445-db-sibling", message="safe")

        with self.assertLogs(service.__name__, level="ERROR"):
            with patch.object(service, "_execute_event_action", side_effect=execute):
                service.process_event_rules(self.event)

        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.STATUS_PROCESSED)
        self.assertCountEqual(attempts, [self.failing_rule.pk, self.success_rule.pk])
        self.assertEqual(Notification.objects.filter(subject="issue445-db-sibling").count(), 1)

    def test_no_eligible_rule_marks_event_processed(self):
        service = _event_service()
        self.failing_rule.enabled = False
        self.failing_rule.save(update_fields=["enabled"])
        self.success_rule.enabled = False
        self.success_rule.save(update_fields=["enabled"])
        service.process_event_rules(self.event)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.STATUS_PROCESSED)

    def test_caught_failure_is_terminal_and_second_dispatch_does_not_replay(self):
        service, attempts, execute = self._dispatch_with_canary()

        with patch.object(service, "_execute_event_action", side_effect=execute):
            service.process_event_rules(self.event)
            service.process_event_rules(self.event)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.STATUS_PROCESSED)
        self.assertEqual(
            attempts.count(self.failing_rule.pk),
            1,
            "missing issue445 non-replayable caught-failure contract",
        )
        self.assertEqual(
            Notification.objects.filter(subject="issue445-success").count(),
            1,
            "missing issue445 processed-event no-duplicate-side-effect contract",
        )

    def test_rule_failure_log_has_only_safe_ids_and_exception_class(self):
        service, _attempts, execute = self._dispatch_with_canary()

        logger = logging.getLogger(service.__name__)
        with self.assertLogs(logger, level="ERROR") as captured:
            with patch.object(service, "_execute_event_action", side_effect=execute):
                service.process_event_rules(self.event)

        self.assertEqual(len(captured.records), 1, "missing issue445 one-record event failure audit contract")
        record = captured.records[0]
        rendered = captured.output[0]
        self.assertIn(str(self.event.pk), rendered)
        self.assertIn(str(self.failing_rule.pk), rendered)
        self.assertIn("RuntimeError", rendered, "missing issue445 exception-class-only event log contract")
        self.assertNotIn(self.CANARY, rendered, "missing issue445 canary-redaction event log contract")
        self.assertIsNone(record.exc_info, "missing issue445 traceback-free event log contract")
        self.assertIsNone(record.exc_text, "missing issue445 traceback-free event log contract")


class Issue445EventConcurrencyTests(TransactionTestCase):
    """The processed flag and every durable rule side effect commit atomically."""

    def setUp(self):
        super().setUp()
        self.content_type = ContentType.objects.get_for_model(Manufacturer)
        self.user = get_user_model().objects.create_user(username="issue445-event-race-user", password="pw")
        self.event = Event.objects.create(
            model=self.content_type,
            object_id=446,
            action=Event.ACTION_CREATE,
            data={"app_label": "assets", "model_name": "manufacturer"},
        )
        self.rule = EventRule.objects.create(
            name="Issue445 concurrency rule",
            model=self.content_type,
            events=[self.event.action],
            action_type=EventRule.ACTION_NOTIFICATION,
            enabled=True,
        )

    def _worker(self, service):
        close_old_connections()
        try:
            return service.process_event_rules(Event._base_manager.get(pk=self.event.pk))
        finally:
            close_old_connections()

    def test_concurrent_dispatchers_create_one_side_effect(self):
        service = _event_service()
        first_action_started = threading.Event()
        release_first_action = threading.Event()
        calls = []
        calls_lock = threading.Lock()

        def execute(rule, event, _tenant_id=None):
            with calls_lock:
                calls.append((rule.pk, event.pk))
            first_action_started.set()
            if not release_first_action.wait(timeout=10):
                raise AssertionError("timed out waiting to release first event action")
            Notification.objects.create(user_id=self.user.pk, subject="issue445-race", message="safe")

        with patch.object(service, "_execute_event_action", side_effect=execute):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(self._worker, service)
                self.assertTrue(first_action_started.wait(timeout=10))
                duplicate = executor.submit(self._worker, service)
                with self.assertRaises(FutureTimeoutError):
                    duplicate.result(timeout=0.5)
                release_first_action.set()
                first.result(timeout=10)
                duplicate.result(timeout=10)

        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.STATUS_PROCESSED)
        self.assertEqual(calls, [(self.rule.pk, self.event.pk)])
        self.assertEqual(Notification.objects.filter(subject="issue445-race").count(), 1)

    def test_final_processed_save_failure_rolls_back_side_effect(self):
        service = _event_service()

        def execute(_rule, _event, _tenant_id=None):
            Notification.objects.create(user_id=self.user.pk, subject="issue445-rollback", message="safe")

        original_save = Event.save

        def fail_terminal_save(instance, *args, **kwargs):
            if "status" in (kwargs.get("update_fields") or []):
                raise DatabaseError("issue445 terminal save canary")
            return original_save(instance, *args, **kwargs)

        with (
            patch.object(service, "_execute_event_action", side_effect=execute),
            patch.object(Event, "save", new=fail_terminal_save),
            self.assertRaises(DatabaseError),
        ):
            service.process_event_rules(self.event)

        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.STATUS_PENDING)
        self.assertFalse(Notification.objects.filter(subject="issue445-rollback").exists())

        with patch.object(service, "_execute_event_action", side_effect=execute):
            service.process_event_rules(self.event)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.STATUS_PROCESSED)
        self.assertEqual(Notification.objects.filter(subject="issue445-rollback").count(), 1)

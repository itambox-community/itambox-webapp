"""Regression tests for race-safe django-q2 schedule registration (WS5-9).

``Schedule`` has no unique constraint on ``func``, so the old
``get_or_create(func=...)`` was not idempotent: repeated/concurrent
registration could leave duplicate schedule rows that fire a task more than
once. ``core.schedules.register_schedule`` (and the app-config post_migrate
handlers that use it) must collapse to exactly one row per func.
"""

from django.apps import apps
from django.test import TestCase
from django_q.models import Schedule

from core.schedules import register_schedule, remove_schedule

CORE_FUNC = "extras.tasks.alerts.evaluate_alert_rules_task"
WEBHOOK_RECOVERY_FUNC = "extras.tasks.webhooks.recover_pending_webhook_deliveries"
SUBSCRIPTION_FUNC = "subscriptions.tasks.check_subscription_expiries_and_reminders"
IMPORT_PREVIEW_CLEANUP_FUNC = "core.tasks.cleanup_import_previews_task"


class RegisterScheduleHelperTests(TestCase):
    """The shared helper itself is idempotent and self-healing."""

    def setUp(self):
        Schedule.objects.all().delete()

    def test_repeated_registration_creates_one_row(self):
        for _ in range(3):
            register_schedule(
                CORE_FUNC,
                defaults={
                    "name": "Daily Alert Rule Evaluation",
                    "schedule_type": Schedule.DAILY,
                    "repeats": -1,
                },
            )
        self.assertEqual(Schedule.objects.filter(func=CORE_FUNC).count(), 1)

    def test_existing_duplicates_are_collapsed(self):
        # Simulate rows left behind by the old racy get_or_create.
        Schedule.objects.create(func=CORE_FUNC, name="dup-1", schedule_type=Schedule.DAILY)
        Schedule.objects.create(func=CORE_FUNC, name="dup-2", schedule_type=Schedule.DAILY)
        Schedule.objects.create(func=CORE_FUNC, name="dup-3", schedule_type=Schedule.DAILY)
        self.assertEqual(Schedule.objects.filter(func=CORE_FUNC).count(), 3)

        register_schedule(
            CORE_FUNC,
            defaults={
                "name": "Daily Alert Rule Evaluation",
                "schedule_type": Schedule.DAILY,
                "repeats": -1,
            },
        )

        rows = Schedule.objects.filter(func=CORE_FUNC)
        self.assertEqual(rows.count(), 1)
        # The survivor keeps the earliest row and gets its defaults refreshed.
        survivor = rows.get()
        self.assertEqual(survivor.name, "Daily Alert Rule Evaluation")
        self.assertEqual(survivor.repeats, -1)

    def test_returns_surviving_instance(self):
        first = register_schedule(CORE_FUNC, defaults={"schedule_type": Schedule.DAILY})
        second = register_schedule(CORE_FUNC, defaults={"schedule_type": Schedule.DAILY})
        self.assertIsNotNone(first)
        self.assertEqual(first.pk, second.pk)


class AppConfigScheduleRegistrationTests(TestCase):
    """Invoking the post_migrate handlers twice leaves one row per func."""

    def setUp(self):
        Schedule.objects.all().delete()

    def _run_handler(self, app_label, method_name):
        config = apps.get_app_config(app_label)
        handler = getattr(config, method_name)
        # post_migrate handlers receive (sender, **kwargs).
        handler(sender=config)

    def test_extras_alert_handler_is_idempotent(self):
        self._run_handler("extras", "_register_alert_schedule")
        self._run_handler("extras", "_register_alert_schedule")
        self.assertEqual(Schedule.objects.filter(func=CORE_FUNC).count(), 1)
        self.assertEqual(Schedule.objects.filter(func=WEBHOOK_RECOVERY_FUNC).count(), 1)
        recovery = Schedule.objects.get(func=WEBHOOK_RECOVERY_FUNC)
        self.assertEqual(recovery.schedule_type, Schedule.MINUTES)
        self.assertEqual(recovery.minutes, 1)

    def test_subscriptions_handler_is_idempotent(self):
        self._run_handler("subscriptions", "_register_subscription_tasks")
        self._run_handler("subscriptions", "_register_subscription_tasks")
        self.assertEqual(Schedule.objects.filter(func=SUBSCRIPTION_FUNC).count(), 1)

    def test_core_handler_registers_retention_and_preview_cleanup_once(self):
        self._run_handler("core", "_register_prune_schedule")
        self._run_handler("core", "_register_prune_schedule")

        self.assertEqual(Schedule.objects.filter(func="core.tasks.prune_changelog_task").count(), 1)
        cleanup = Schedule.objects.get(func=IMPORT_PREVIEW_CLEANUP_FUNC)
        self.assertEqual(cleanup.schedule_type, Schedule.HOURLY)
        self.assertEqual(cleanup.repeats, -1)

    def test_no_duplicate_schedules_across_funcs(self):
        # Run every registering handler twice; each func must end with one row.
        self._run_handler("extras", "_register_alert_schedule")
        self._run_handler("subscriptions", "_register_subscription_tasks")
        self._run_handler("extras", "_register_alert_schedule")
        self._run_handler("subscriptions", "_register_subscription_tasks")

        for func in (CORE_FUNC, WEBHOOK_RECOVERY_FUNC, SUBSCRIPTION_FUNC):
            self.assertEqual(
                Schedule.objects.filter(func=func).count(),
                1,
                msg=f"expected exactly one schedule row for {func}",
            )


class RemoveScheduleHelperTests(TestCase):
    """Name-keyed removal deletes exactly the matching rows and never raises."""

    def setUp(self):
        Schedule.objects.all().delete()

    def test_name_filtered_removal_deletes_only_the_matching_row(self):
        Schedule.objects.create(func=CORE_FUNC, name="scheduled_report_1", schedule_type=Schedule.DAILY)
        Schedule.objects.create(func=CORE_FUNC, name="scheduled_report_2", schedule_type=Schedule.DAILY)

        removed = remove_schedule(CORE_FUNC, name="scheduled_report_1")

        self.assertEqual(removed, 1)
        self.assertFalse(Schedule.objects.filter(func=CORE_FUNC, name="scheduled_report_1").exists())
        self.assertTrue(Schedule.objects.filter(func=CORE_FUNC, name="scheduled_report_2").exists())

    def test_unfiltered_removal_removes_every_row_for_the_func(self):
        Schedule.objects.create(func=CORE_FUNC, name="scheduled_report_1", schedule_type=Schedule.DAILY)
        Schedule.objects.create(func=CORE_FUNC, name="scheduled_report_2", schedule_type=Schedule.DAILY)

        removed = remove_schedule(CORE_FUNC)

        self.assertEqual(removed, 2)
        self.assertFalse(Schedule.objects.filter(func=CORE_FUNC).exists())

    def test_removal_is_idempotent_for_unknown_identities(self):
        self.assertEqual(remove_schedule(CORE_FUNC, name="never-registered"), 0)
        self.assertEqual(remove_schedule(CORE_FUNC, name="never-registered"), 0)

    def test_removal_failures_are_swallowed_and_reported_as_zero(self):
        self.assertEqual(remove_schedule(CORE_FUNC, using="not-a-database-alias"), 0)

    def test_registration_failures_are_swallowed_and_return_none(self):
        self.assertIsNone(register_schedule(CORE_FUNC, using="not-a-database-alias"))

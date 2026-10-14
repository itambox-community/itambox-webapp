"""Operator surfaces of the scheduled-report delivery contract.

Every service-level retry outcome has to reach the operator as a specific
message, the trigger view has to surface the recorded delivery detail, and
deactivating a schedule has to unregister it. These surfaces are what makes
the Stable promotion operator-safe; they are asserted here rather than in
the delivery drill because they exercise the views, not the tasks.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.messages import constants as message_constants
from django.contrib.messages import get_messages
from django.http import Http404
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags
from django_q.models import Schedule

from core.tables.constants import TABLE_EMPTY_VALUE
from extras.models import ReportGenerationArchive, ReportTemplate, ScheduledReport
from extras.tables import ScheduledReportTable
from extras.views import ScheduledReportRetryDeliveryView, handle_report_scheduling

User = get_user_model()

RETRY_URL = "extras:scheduledreport_retry_delivery"
TRIGGER_URL = "extras:scheduledreport_trigger"


class RetryDeliveryViewTests(TestCase):
    """Each service outcome renders its own actionable operator message."""

    def setUp(self):
        from organization.models import Tenant

        self.tenant = Tenant.objects.create(name="Retry View Tenant", slug="retry-view-tenant")
        self.user = User.objects.create_user(username="retryview", password="password123", is_superuser=True)
        self.template = ReportTemplate.objects.create(
            name="Retry View Report",
            report_type=ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
            included_columns=["asset_tag"],
        )
        self.sched = ScheduledReport.objects.create(
            name="Retry View Schedule",
            report=self.template,
            tenant=self.tenant,
            frequency=ScheduledReport.FREQUENCY_DAILY,
            format=ScheduledReport.FORMAT_HTML,
            recipients="retry-view@example.com",
        )
        self.client.force_login(self.user)

    @patch("extras.views.retry_failed_deliveries")
    def test_retry_outcome_codes_render_the_operator_message(self, mock_retry):
        cases = (
            ("retry.no_archive", "no retained archived output", message_constants.WARNING),
            ("retry.no_recorded_failures", "no recorded failed deliveries", message_constants.INFO),
            ("retry.no_retained_output", "could not read the retained archive output", message_constants.WARNING),
            ("retry.inactive", "the schedule is inactive", message_constants.ERROR),
            ("retry.in_progress", "already in progress", message_constants.INFO),
            ("retry.scope_unauthorized", "no longer covered by a current approval", message_constants.ERROR),
            ("retry.completed", "retried successfully", message_constants.SUCCESS),
        )
        for code, needle, level in cases:
            with self.subTest(code=code):
                mock_retry.return_value = SimpleNamespace(code=code, detail="")
                response = self.client.post(reverse(RETRY_URL, kwargs={"pk": self.sched.pk}))
                self.assertEqual(response.status_code, 302)
                mock_retry.assert_called_with(self.sched)
                rendered = [(message.level, str(message)) for message in get_messages(response.wsgi_request)]
                self.assertTrue(
                    any(level == actual_level and needle in text for actual_level, text in rendered),
                    f"{code!r} rendered {rendered!r}",
                )

    @patch("extras.views.retry_failed_deliveries")
    def test_partial_retry_renders_the_still_failing_detail(self, mock_retry):
        mock_retry.return_value = SimpleNamespace(
            code="retry.partial",
            detail="Email: email.delivery_failed",
        )
        response = self.client.post(reverse(RETRY_URL, kwargs={"pk": self.sched.pk}))

        self.assertEqual(response.status_code, 302)
        rendered = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertTrue(any("left targets failing" in text and "email.delivery_failed" in text for text in rendered))


class ScheduledReportRetryDeliveryPermissionTests(SimpleTestCase):
    """The retry action is gated exactly like the other operator actions."""

    def test_scoped_miss_denies_without_model_permission_fallback(self):
        view = ScheduledReportRetryDeliveryView()
        view.request = Mock()
        view.request.user = Mock()
        view.kwargs = {"pk": 42}

        with patch("extras.views.get_object_or_404", side_effect=Http404):
            self.assertFalse(view.has_permission())

        view.request.user.has_perms.assert_not_called()

    def test_user_without_the_change_permission_is_denied(self):
        view = ScheduledReportRetryDeliveryView()
        view.request = Mock()
        view.request.user = Mock()
        view.request.user.has_perms.return_value = False
        view.kwargs = {"pk": 42}

        with patch("extras.views.get_object_or_404", return_value=Mock()):
            self.assertFalse(view.has_permission())


class TriggerDeliveryDetailTests(TestCase):
    """The trigger view surfaces the recorded delivery detail per outcome."""

    def setUp(self):
        from organization.models import Tenant

        self.tenant = Tenant.objects.create(name="Trigger View Tenant", slug="trigger-view-tenant")
        self.user = User.objects.create_user(username="triggerview", password="password123", is_superuser=True)
        self.template = ReportTemplate.objects.create(
            name="Trigger View Report",
            report_type=ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
            included_columns=["asset_tag"],
        )
        self.sched = ScheduledReport.objects.create(
            name="Trigger View Schedule",
            report=self.template,
            tenant=self.tenant,
            frequency=ScheduledReport.FREQUENCY_DAILY,
            format=ScheduledReport.FORMAT_HTML,
            recipients="trigger-view@example.com",
        )
        self.client.force_login(self.user)

    def _trigger(self, *, status, archive=None, success=True):
        def side_effect(sched_pk, invoked_by_user_id=None):
            ScheduledReport.objects.filter(pk=sched_pk).update(last_status=status)
            return success

        if archive is not None:
            archive()
        with patch("extras.views.generate_scheduled_report_task", side_effect=side_effect):
            response = self.client.post(reverse(TRIGGER_URL, kwargs={"pk": self.sched.pk}))
        self.assertEqual(response.status_code, 302)
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_partial_delivery_prefers_the_recorded_ledger(self):
        def add_archive():
            ReportGenerationArchive.objects.create(
                scheduled_report=self.sched,
                format=ScheduledReport.FORMAT_HTML,
                status="succeeded",
                tenant=self.tenant,
                delivery_status="partial",
                delivery_targets=[
                    {"target": "email", "label": "Email", "status": "failed", "error": "email.delivery_failed"},
                    {"target": "channel:1", "label": "Ops", "status": "ok", "error": ""},
                ],
            )

        rendered = self._trigger(status="partial", archive=add_archive)
        self.assertTrue(
            any(
                "delivered only partially" in text and "Email: failed (email.delivery_failed)" in text
                for text in rendered
            )
        )

    def test_partial_delivery_without_an_archive_falls_back_to_check_logs(self):
        rendered = self._trigger(status="partial")
        self.assertTrue(any("delivered only partially" in text and "Check logs." in text for text in rendered))

    def test_failed_delivery_prefers_the_recorded_ledger(self):
        def add_archive():
            ReportGenerationArchive.objects.create(
                scheduled_report=self.sched,
                format=ScheduledReport.FORMAT_HTML,
                status="succeeded",
                tenant=self.tenant,
                delivery_status="failed",
                delivery_targets=[
                    {"target": "email", "label": "Email", "status": "failed", "error": "email.delivery_rejected"},
                ],
            )

        rendered = self._trigger(status="failed", archive=add_archive)
        self.assertTrue(
            any(
                "all deliveries failed" in text and "Email: failed (email.delivery_rejected)" in text
                for text in rendered
            )
        )

    def test_generation_failure_reports_the_token_detail(self):
        rendered = self._trigger(status="generation: disk full", success=False)
        self.assertTrue(any("Failed to generate" in text and "disk full" in text for text in rendered))

    def test_generation_failure_falls_back_to_the_archive_error(self):
        def add_archive():
            ReportGenerationArchive.objects.create(
                scheduled_report=self.sched,
                format=ScheduledReport.FORMAT_HTML,
                status="failed",
                tenant=self.tenant,
                error_message="storage backend unavailable",
            )

        rendered = self._trigger(status="", archive=add_archive, success=False)
        self.assertTrue(
            any("Failed to generate" in text and "storage backend unavailable" in text for text in rendered)
        )

    def test_generation_failure_without_any_detail_says_check_logs(self):
        rendered = self._trigger(status="", success=False)
        self.assertTrue(any("Failed to generate" in text and "Check logs." in text for text in rendered))


class HandleReportSchedulingSyncTests(TestCase):
    """Deactivation clears the FK and unregisters the django-q row."""

    def setUp(self):
        from organization.models import Tenant

        self.tenant = Tenant.objects.create(name="Sync Tenant", slug="sync-tenant")
        self.template = ReportTemplate.objects.create(
            name="Sync Report",
            report_type=ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
            included_columns=["asset_tag"],
        )

    def test_deactivating_a_schedule_clears_and_unregisters_it(self):
        sched = ScheduledReport.objects.create(
            name="Deactivation Schedule",
            report=self.template,
            tenant=self.tenant,
            frequency=ScheduledReport.FREQUENCY_DAILY,
            format=ScheduledReport.FORMAT_HTML,
            recipients="sync@example.com",
            is_active=True,
        )
        handle_report_scheduling(sched)
        sched.refresh_from_db()
        self.assertIsNotNone(sched.schedule_id)
        self.assertTrue(Schedule.objects.filter(name=f"scheduled_report_{sched.pk}").exists())

        sched.is_active = False
        sched.save(update_fields=["is_active"])
        handle_report_scheduling(sched)

        sched.refresh_from_db()
        self.assertIsNone(sched.schedule_id)
        self.assertFalse(Schedule.objects.filter(name=f"scheduled_report_{sched.pk}").exists())


class ScheduledReportTableRenderTests(TestCase):
    """The list's next-run column renders a timestamp or the empty marker."""

    def setUp(self):
        from organization.models import Tenant

        self.tenant = Tenant.objects.create(name="Table Tenant", slug="table-tenant")
        self.template = ReportTemplate.objects.create(
            name="Table Report",
            report_type=ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
            included_columns=["asset_tag"],
        )

    def test_next_run_column_renders_time_or_dash(self):
        registered = ScheduledReport.objects.create(
            name="Registered Schedule",
            report=self.template,
            tenant=self.tenant,
            frequency=ScheduledReport.FREQUENCY_HOURLY,
            format=ScheduledReport.FORMAT_HTML,
            recipients="table@example.com",
            is_active=True,
        )
        handle_report_scheduling(registered, reanchor=False)

        dormant = ScheduledReport.objects.create(
            name="Dormant Schedule",
            report=self.template,
            tenant=self.tenant,
            frequency=ScheduledReport.FREQUENCY_HOURLY,
            format=ScheduledReport.FORMAT_HTML,
            recipients="table@example.com",
            is_active=True,
        )

        table = ScheduledReportTable([])
        registered.refresh_from_db()
        self.assertIsNotNone(registered.schedule)
        Schedule.objects.filter(pk=registered.schedule_id).update(next_run=timezone.now())
        registered.refresh_from_db()

        rendered = str(table.render_next_run(registered))
        self.assertRegex(rendered, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

        rendered_empty = str(table.render_next_run(dormant))
        self.assertEqual(strip_tags(rendered_empty).strip(), TABLE_EMPTY_VALUE)

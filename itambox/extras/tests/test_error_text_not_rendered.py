"""Exception text must not reach the user interface.

Library and widget failures are logged for operators; what a user sees is a
fixed, translatable message that does not echo the exception.
"""

from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase

from extras.models import ScheduledReport
from extras.templatetags.dashboard import render_widget


class TestScheduledReportCronMessage(TestCase):
    def _clean(self, expression):
        return ScheduledReport(frequency=ScheduledReport.FREQUENCY_CRON, cron_expression=expression).clean()

    def test_malformed_expression_gets_fixed_message_without_library_text(self):
        for expression in ("not a cron", "99 99 99 99 99", "* * *"):
            with self.subTest(expression=expression), self.assertRaises(ValidationError) as caught:
                self._clean(expression)
            messages = caught.exception.message_dict["cron_expression"]
            self.assertEqual(len(messages), 1)
            self.assertIn("Invalid Cron expression", messages[0])
            self.assertNotIn(expression, messages[0])
            self.assertNotIn("not acceptable", messages[0])

    def test_valid_expression_passes(self):
        self._clean("0 8 * * 1-5")

    def test_unexpected_croniter_failure_is_not_converted_into_a_form_error(self):
        with patch("extras.models.croniter", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            self._clean("0 8 * * 1-5")


class _ExplodingWidget:
    def __init__(self, config):
        self.config = config

    def render(self, request):
        raise RuntimeError("SECRET-INTERNAL-DETAIL /srv/app/secret.py")


class TestRenderWidgetDoesNotLeakExceptionText(SimpleTestCase):
    def test_failing_widget_renders_opaque_marker_and_logs_traceback(self):
        context = {"request": RequestFactory().get("/")}
        with (
            patch("extras.templatetags.dashboard.get_widget", return_value=_ExplodingWidget),
            self.assertLogs("extras.templatetags.dashboard", level="ERROR") as logs,
        ):
            output = render_widget(context, {"widget": "exploding"})
        self.assertEqual(output, "<!-- Widget error (exploding) -->")
        self.assertNotIn("SECRET-INTERNAL-DETAIL", output)
        self.assertIn("SECRET-INTERNAL-DETAIL", "\n".join(logs.output))

from django.test import SimpleTestCase

from core.context import reset_current_csp_nonce, set_current_csp_nonce
from core.reports.charts import generate_bar_chart, generate_doughnut_chart


class ReportChartHTMLTests(SimpleTestCase):
    def test_chart_uses_classes_and_nonce_style_block(self):
        token = set_current_csp_nonce("chart-nonce")
        try:
            rendered = generate_doughnut_chart([{"label": "Active", "value": 2}], title="Status")
        finally:
            reset_current_csp_nonce(token)

        self.assertNotIn(" style=", rendered)
        self.assertIn('class="report-chart report-chart-doughnut"', rendered)
        self.assertIn('<style nonce="chart-nonce">', rendered)

    def test_chart_labels_titles_and_displays_are_escaped(self):
        payload = "</text><script>alert(1)</script><img src=x onerror=alert(2)>"
        charts = (
            generate_doughnut_chart([{"label": payload, "value": 1}], title=payload),
            generate_bar_chart([{"label": payload, "value": 1, "display": payload}]),
        )
        for index, rendered in enumerate(charts):
            with self.subTest(chart=index):
                self.assertNotIn("<script>", rendered)
                self.assertNotIn("<img", rendered)
                self.assertNotIn("</text><script>", rendered)
                self.assertIn("&lt;script&gt;", rendered)
                self.assertIn("&lt;/text&gt;", rendered)

    def test_chart_without_request_nonce_fails_closed(self):
        rendered = generate_doughnut_chart([{"label": "Assets", "value": 1}])

        self.assertNotIn("<style", rendered.lower())
        self.assertIn('class="report-chart report-chart-doughnut"', rendered)

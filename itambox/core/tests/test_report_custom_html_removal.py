import csv
import io
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase
from jinja2.exceptions import SecurityError

from core.reports.rendering import render_report_csv, render_report_html
from extras.forms import ReportTemplateForm
from extras.models import ReportTemplate
from extras.views import ReportTemplatePreviewView


class ReportCustomHTMLRemovalTests(TestCase):
    def test_report_model_and_form_keep_custom_html_without_retired_shape_fields(self):
        fields = {field.name: field for field in ReportTemplate._meta.get_fields()}

        self.assertNotIn("advanced_mode", fields)
        self.assertNotIn("legacy_designer_grandfathered", fields)
        self.assertIn("template_content", fields)

        form = ReportTemplateForm()

        self.assertNotIn("advanced_mode", form.fields)
        self.assertNotIn("legacy_designer_grandfathered", form.fields)
        self.assertIn("template_content", form.fields)

    def test_canonical_csv_uses_columns_and_disclosure(self):
        csv_text = render_report_csv(["Asset Tag"], [{"Asset Tag": "A-1", "Ignored": "value"}], "Sample output")

        self.assertEqual(
            list(csv.reader(io.StringIO(csv_text))),
            [["Asset Tag"], ["A-1"], [], ["Sample output"]],
        )

    def test_custom_html_keeps_autoescaping_and_sandbox_boundary(self):
        rendered = render_report_html(
            {"report_name": "<report>"},
            SimpleNamespace(template_content="<h1>{{ report_name }}</h1>"),
        )
        self.assertIn("&lt;report&gt;", rendered)

        # Sandboxed attribute access yields an unsafe undefined first; the
        # SecurityError surfaces as soon as the blocked value is used.
        with self.assertRaises(SecurityError):
            render_report_html(
                {"report_name": "Report"},
                SimpleNamespace(template_content="{{ report_name.__class__.mro }}"),
            )

    def test_rendered_preview_iframe_keeps_sandbox_attribute(self):
        class FrameParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.frames = []

            def handle_starttag(self, tag, attrs):
                if tag == "iframe":
                    self.frames.append(dict(attrs))

        request = RequestFactory().get("/extras/reports/templates/1/edit/")
        request.user = AnonymousUser()
        html = render_to_string(
            "core/reports/report_template_form.html",
            {"form": ReportTemplateForm(), "object": SimpleNamespace(included_columns=[]), "title": "Preview"},
            request=request,
        )

        parser = FrameParser()
        parser.feed(html)
        self.assertEqual(len(parser.frames), 1)
        self.assertEqual(parser.frames[0].get("sandbox"), "allow-same-origin")

    def test_non_superuser_preview_uses_the_active_tenant(self):
        active_tenant = SimpleNamespace(pk=7)
        request = RequestFactory().post(
            "/extras/reports/templates/preview/",
            {"report_type": "asset_summary", "tenant": "999", "template_content": ""},
        )
        request.user = SimpleNamespace(is_superuser=False)
        context_data = {}

        with (
            patch("extras.views.get_current_tenant", return_value=active_tenant) as get_active_tenant,
            patch("extras.views._specification_inputs", return_value=([], [])),
            patch.object(ReportTemplate, "full_clean"),
            patch("core.reports.build_report_context", return_value=([], [], [], [], None, context_data)) as build,
            patch("extras.views.render_report_html", return_value="<html></html>"),
        ):
            response = ReportTemplatePreviewView().post(request)

        self.assertEqual(response.status_code, 200)
        get_active_tenant.assert_called_once_with()
        self.assertIs(build.call_args.kwargs["active_tenant"], active_tenant)
        self.assertEqual(build.call_args.kwargs["filter_tenants"], [])

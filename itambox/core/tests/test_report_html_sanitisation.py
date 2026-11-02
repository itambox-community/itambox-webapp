"""Stored report templates must not emit executable markup (#997)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.mail import EmailMessage
from django.test import RequestFactory, SimpleTestCase
from jinja2.exceptions import SecurityError, TemplateError

from core.reports.rendering import render_report_html, sanitize_report_html
from core.sandbox import UNSAFE_FILTERS, UNSAFE_GLOBALS, hardened_sandbox_environment
from extras.models import ExportTemplate
from extras.tasks.reports import _render_report_html
from extras.views import ReportTemplateDownloadView, ReportTemplatePreviewView

HOSTILE = (
    "<h1>{{ report_name }}</h1><script>alert(1)</script>"
    '<img src="x" onerror="alert(2)"><a href="javascript:alert(3)">go</a>'
    '<a href="https://example.com/ok" onclick="alert(4)">ok</a>'
    '<iframe src="https://evil.example"></iframe><form action="/x"><input name="a"></form>'
    "<style>body{display:none}</style><p>kept</p>"
)
BUILD = ([], [], [], {}, "", {"report_name": "Quarterly"})


def assert_inert(html):
    lowered = html.lower()
    for needle in ("<script", "alert(", "onerror", "onclick", "javascript:", "<iframe", "<form", "<input", "<style"):
        assert needle not in lowered, (needle, html)
    assert "<p>kept</p>" in html
    assert "Quarterly" in html


def hostile_template():
    return SimpleNamespace(template_content=HOSTILE)


class SanitizerTests(SimpleTestCase):
    def test_removes_script_handlers_and_unsafe_urls(self):
        assert_inert(render_report_html({"report_name": "Quarterly"}, hostile_template()))

    def test_benign_custom_template_is_preserved(self):
        template = SimpleNamespace(
            template_content=(
                '<table class="t"><tr><th>Tag</th></tr>{% for h in headers %}<tr><td>{{ h }}</td></tr>'
                '{% endfor %}</table><a href="https://example.com/x">link</a><strong>b</strong>'
            )
        )
        out = render_report_html({"headers": ["A-1"]}, template)
        assert "<td>A-1</td>" in out
        assert 'href="https://example.com/x"' in out
        assert "<strong>b</strong>" in out
        assert "<table" in out

    def test_interpolated_values_stay_escaped(self):
        template = SimpleNamespace(template_content="<p>{{ description }}</p>")
        out = render_report_html({"description": "<script>x</script>"}, template)
        assert "<script" not in out
        assert "&lt;script" in out

    def test_empty_input(self):
        assert sanitize_report_html("") == ""

    def test_script_text_is_dropped_with_nested_and_empty_tags(self):
        out = sanitize_report_html("<p>a</p><script>1<script>2</script>3</script><p>b</p>")
        assert "<script" not in out
        assert "<p>a</p>" in out and "<p>b</p>" in out
        assert sanitize_report_html("<svg><script/></svg><p>c</p>").endswith("<p>c</p>")


class SandboxParityTests(SimpleTestCase):
    def test_removed_filters_and_globals(self):
        env = hardened_sandbox_environment()
        for name in UNSAFE_FILTERS:
            assert name not in env.filters
        for name in UNSAFE_GLOBALS:
            assert name not in env.globals

    def test_export_template_uses_same_hardening(self):
        env = ExportTemplate(name="x", template_code="").get_jinja_environment()
        for name in UNSAFE_FILTERS:
            assert name not in env.filters
        for name in UNSAFE_GLOBALS:
            assert name not in env.globals

    def test_report_templates_reject_gadgets(self):
        for source in ("{{ [1]|map('string')|list }}", "{{ namespace() }}", "{{ lipsum() }}", "{{ 'a'|format() }}"):
            with pytest.raises(TemplateError):
                render_report_html({}, SimpleNamespace(template_content=source))

    def test_immutable_sandbox_blocks_mutation(self):
        with pytest.raises(SecurityError):
            render_report_html({}, SimpleNamespace(template_content="{{ ''.__class__.__mro__ }}"))


class SinkTests(SimpleTestCase):
    def test_preview(self):
        request = RequestFactory().post(
            "/preview/",
            {"report_type": "asset_summary", "included_columns": ["asset_tag"], "template_content": HOSTILE},
        )
        request.user = SimpleNamespace(is_superuser=False)
        with (
            patch("extras.views.get_current_tenant", return_value=None),
            patch("core.reports.build_report_context", return_value=BUILD),
        ):
            response = ReportTemplatePreviewView().post(request)
        assert response.status_code == 200
        assert_inert(response.content.decode())

    def _download(self, **params):
        template = SimpleNamespace(
            name="Hostile",
            filter_tenants=SimpleNamespace(all=lambda: []),
            persisted_filter_tenant_ids=lambda: [],
            report_type="asset_summary",
            template_content=HOSTILE,
        )
        with (
            patch("extras.views.get_object_or_404", return_value=template),
            patch("extras.views.get_current_tenant", return_value=None),
            patch("core.reports.build_report_context", return_value=BUILD),
        ):
            return ReportTemplateDownloadView().get(SimpleNamespace(GET=params), 1)

    def test_html_download(self):
        response = self._download(format="html")
        assert_inert(response.content.decode())

    def test_print_download(self):
        response = self._download(format="html", print="true")
        assert response["Content-Disposition"].startswith("inline;")
        assert_inert(response.content.decode())

    def test_scheduled_email_body(self):
        body = _render_report_html({"report_name": "Quarterly"}, hostile_template())
        assert_inert(body)
        message = EmailMessage(subject="s", body=body, to=["a@example.com"])
        message.content_subtype = "html"
        assert_inert(message.message().get_payload())

    def test_pdf_input_is_sanitised(self):
        with patch("extras.views.report_pdf_bytes", return_value=b"pdf") as pdf:
            self._download(format="pdf")
        assert_inert(pdf.call_args.args[0])

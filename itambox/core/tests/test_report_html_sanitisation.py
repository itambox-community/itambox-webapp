"""Stored report templates must not emit executable markup (#997)."""

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.mail import EmailMessage
from django.test import RequestFactory, SimpleTestCase
from jinja2.exceptions import SecurityError, TemplateError

from core.reports.rendering import (
    MAX_RENDERED_CHARS,
    MAX_TEMPLATE_CHARS,
    RENDER_DEADLINE_SECONDS,
    ReportRenderLimitError,
    render_report_html,
    sanitize_report_html,
)
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


PNG = "data:image/png;base64,iVBORw0KGgo="


@pytest.mark.parametrize(
    "source",
    [
        '<a href="https://example.com/x">l</a>',
        '<a href="http://example.com/x">l</a>',
        '<a href="mailto:a@example.com">l</a>',
        '<a href="tel:+491234">l</a>',
        '<a href="/relative/path">l</a>',
        '<a href="#anchor">l</a>',
        '<img src="https://example.com/i.png" alt="i" width="3" height="4">',
        '<img src="/static/i.png">',
        '<img src="%s">' % PNG,
        '<table><tr><td colspan="2">c</td></tr></table>',
        '<p class="x" title="t" lang="de" dir="ltr" aria-label="a">p</p>',
    ],
)
def test_allowed_markup_survives(source):
    out = sanitize_report_html(source)
    for token in ("href=", "src=", "colspan=", "class="):
        if token in source:
            assert token in out, (source, out)


@pytest.mark.parametrize(
    ("source", "attr"),
    [
        ('<a href="javascript:alert(1)">x</a>', "href"),
        ('<a href="JaVaScRiPt:alert(1)">x</a>', "href"),
        ('<a href="vbscript:msgbox(1)">x</a>', "href"),
        ('<a href="data:text/html;base64,PHNjcmlwdD4=">x</a>', "href"),
        ('<a href="java&#10;script:alert(1)">x</a>', "href"),
        ('<a href="java&#9;script:alert(1)">x</a>', "href"),
        ('<a href="">x</a>', "href"),
        ('<a href="http://[::1">x</a>', "href"),
        ('<img src="data:text/html;base64,PHNjcmlwdD4=">', "src"),
        ('<img src="data:image/svg+xml;base64,PHN2Zz4=">', "src"),
        ('<img src="javascript:alert(1)">', "src"),
        ('<img src="mailto:a@example.com">', "src"),
        ('<img src="">', "src"),
        ('<p onclick="alert(1)">x</p>', "onclick"),
        ('<a href="https://example.com" onmouseover="alert(1)">x</a>', "onmouseover"),
        ('<p style="display:none">x</p>', "style"),
        ('<table><tr><td data-x="1" colspan="2">x</td></tr></table>', "data-x"),
        ('<p colspan="2">x</p>', "colspan"),
    ],
    ids=[
        "javascript",
        "javascript-mixed-case",
        "vbscript",
        "data-html",
        "javascript-newline",
        "javascript-tab",
        "empty-href",
        "ipv6-literal",
        "img-data-html",
        "img-data-svg",
        "img-javascript",
        "img-mailto",
        "img-empty",
        "onclick",
        "onmouseover",
        "style",
        "data-attribute",
        "stray-colspan",
    ],
)
def test_unsafe_attributes_are_removed(source, attr):
    out = sanitize_report_html(source)
    assert attr + "=" not in out.lower(), (source, out)
    assert "x</" in out or "<img" in out


def test_event_handler_removed_but_allowed_attribute_kept():
    out = sanitize_report_html('<a href="https://example.com/ok" onclick="alert(1)">ok</a>')
    assert 'href="https://example.com/ok"' in out
    assert "onclick" not in out


@pytest.mark.parametrize("tag", ["script", "style", "iframe", "svg", "form", "object", "template"])
def test_drop_elements_removed_with_text_and_tail_kept(tag):
    out = sanitize_report_html("<p>before</p>lead<%s>SECRET</%s>tail<p>after</p>" % (tag, tag))
    assert "SECRET" not in out and "<" + tag not in out
    assert out.index("before") < out.index("lead") < out.index("tail") < out.index("after")


def test_drop_element_first_child_and_only_child_keeps_tail():
    out = sanitize_report_html("<div><script>x</script>tail</div>")
    assert "<div>tail</div>" in out
    out = sanitize_report_html("<div><b>a</b><script>x</script>tail</div>")
    assert "<b>a</b>tail" in out
    assert sanitize_report_html("<script>x</script>top").endswith("top")


def test_unknown_element_is_unwrapped_keeping_content():
    out = sanitize_report_html("<p>a</p><blink>lead<b>bold</b>mid<i>it</i>end</blink>z")
    assert "blink" not in out
    for text in ("lead", "bold", "mid", "it", "end", "z"):
        assert text in out
    assert out.index("lead") < out.index("bold") < out.index("mid") < out.index("end") < out.index("z")


def test_unknown_element_leading_text_after_sibling_and_at_start():
    out = sanitize_report_html("<b>x</b><center>lead<em>e</em></center>")
    assert "center" not in out and out.index("x") < out.index("lead") < out.index("e</")
    out = sanitize_report_html("<center>only</center>")
    assert out == "only"
    out = sanitize_report_html("<center><script>s</script>t</center>")
    assert "s</" not in out and "t" in out


def test_comments_and_empty_templates_roundtrip():
    assert sanitize_report_html(None) == ""
    assert sanitize_report_html("<!-- only a comment -->") == ""
    out = sanitize_report_html("a<!-- c -->b<p>c</p><!-- d -->tail")
    assert "<!--" not in out
    assert out.replace("<p>", "").replace("</p>", "").replace(" ", "") != ""
    for text in ("a", "b", "c", "tail"):
        assert text in out


def test_nested_elements_and_tails_keep_benign_text_in_order():
    out = sanitize_report_html("<div>1<p>2<b>3</b>4</p>5<ul><li>6</li>7</ul>8</div>9")
    positions = [out.index(str(n)) for n in range(1, 10)]
    assert positions == sorted(positions)


# --- RS-1..RS-3, RS-5: render budgets, depth, span clamping, link targets --------------------------------------------


def render_source(source, context=None):
    return render_report_html(context or {}, SimpleNamespace(template_content=source))


def test_deeply_nested_html_is_rejected_quickly_without_recursion_error():
    started = time.monotonic()
    with pytest.raises(ReportRenderLimitError):
        sanitize_report_html("<div>" * 1200)
    with pytest.raises(ReportRenderLimitError):
        render_source("{% for i in range(2000) %}<div>{% endfor %}")
    assert time.monotonic() - started < 10


def test_reasonably_nested_html_still_sanitises():
    out = sanitize_report_html("<div>" * 900 + "deep" + "</div>" * 900)
    assert "deep" in out and out.count("<div>") == 900


def test_oversized_html_is_rejected_before_parsing():
    with pytest.raises(ReportRenderLimitError):
        sanitize_report_html("a" * (MAX_RENDERED_CHARS + 1))


def test_span_attributes_are_clamped_and_invalid_values_dropped():
    out = sanitize_report_html(
        '<table><colgroup span="99999"></colgroup><col span="0"><tr>'
        '<td colspan="2000000" rowspan="9999999">a</td><td colspan="x" rowspan="-3">b</td>'
        '<th colspan="3" rowspan="2">c</th><td colspan="0">d</td></tr></table>'
    )
    assert 'colspan="1000"' in out and 'rowspan="65534"' in out
    assert 'span="99999"' not in out and 'colspan="x"' not in out and 'rowspan="-3"' not in out
    assert 'colspan="3"' in out and 'rowspan="2"' in out
    assert 'colspan="0"' not in out and 'span="0"' not in out
    assert 'span="1000"' in out


@pytest.mark.parametrize("href", ["\\evil.example\share", "//evil.example/x", "/\evil", "\evil"])
def test_network_path_links_are_removed(href):
    out = sanitize_report_html('<a href="' + href + '">x</a>')
    assert "href" not in out and "evil" not in out


def test_safe_link_targets_are_preserved():
    out = sanitize_report_html(
        '<a href="/path">a</a><a href="#frag">b</a><a href="https://ok.example/">c</a>'
        '<a href="mailto:a@b.example">d</a><a href="tel:+491">e</a>'
    )
    for target in ("/path", "#frag", "https://ok.example/", "mailto:a@b.example", "tel:+491"):
        assert 'href="' + target + '"' in out


@pytest.mark.parametrize(
    "source",
    [
        "{% for a in range(100000) %}{% for b in range(1000) %}x{% endfor %}{% endfor %}",
        "{{ ([0] * 100000000)|length }}",
        "{{ 'a' * 100000000 }}",
        "{{ 10 ** 10 ** 10 }}",
        "{{ x ** y }}",
        "{{ 'a'.ljust(100000000) }}",
        "{{ 'a'|center(100000000) }}",
        "{{ '%0100000d' % 1 }}",
    ],
)
def test_resource_exhaustion_payloads_fail_fast(source):
    started = time.monotonic()
    with pytest.raises(ReportRenderLimitError):
        render_source(source, {"x": 9, "y": 10**9})
    assert time.monotonic() - started < RENDER_DEADLINE_SECONDS + 10


def test_deadline_interrupts_pure_python_loop_and_leaves_thread_usable():
    with patch("core.reports.rendering.RENDER_DEADLINE_SECONDS", 0.5):
        with pytest.raises(ReportRenderLimitError):
            render_source("{% for a in range(100000) %}{% for b in range(100000) %}{% endfor %}{% endfor %}")
    assert render_source("<p>{{ 1 + 1 }}</p>") == "<p>2</p>"


def test_oversized_template_source_is_rejected():
    with pytest.raises(ReportRenderLimitError):
        render_source("x" * (MAX_TEMPLATE_CHARS + 1))


def test_normal_loop_table_with_small_repetition_renders():
    out = render_source(
        "<table>{% for h in headers %}<tr><td>{{ h }}</td></tr>{% endfor %}</table><p>{{ '-' * 20 }}{{ 2 ** 10 }}</p>",
        {"headers": ["A", "B"]},
    )
    assert "<td>A</td>" in out and "<td>B</td>" in out and "-" * 20 + "1024" in out

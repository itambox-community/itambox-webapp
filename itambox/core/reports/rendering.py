"""Shared report rendering policy for workers and HTTP exports."""

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlsplit

from django.template import Context, Template
from django.utils.html import escape
from django.utils.translation import gettext as _

from core.csv_utils import csv_safe
from core.reports.templates import get_polished_system_html_template
from core.sandbox import hardened_sandbox_environment


def report_disclosure_text(result, row_limit, truncated_template=None):
    """One sentence describing a capped or sample-only output, or ``""``.

    Truncation always carries its determinate total: the compiler counts the
    scope whenever the row window is full, so the sentence can state both
    numbers.  The same sentence reaches every output surface (HTML/PDF banner,
    CSV/XLSX trailer, scheduled mail body) so no format quietly implies the
    window is the full population.  ``truncated_template`` lets a provider
    whose window is not a single first-N slice (hardware inventory) state its
    real shape.
    """
    if getattr(result, "is_sample", False):
        return _("No records matched this scope; this output shows the report's sample data.")
    if getattr(result, "truncated", False):
        if truncated_template:
            return _(truncated_template) % {
                "limit": row_limit,
                "total": getattr(result, "total_rows", None),
            }
        return _("Showing the first %(limit)s of %(total)s matching rows.") % {
            "limit": row_limit,
            "total": getattr(result, "total_rows", None),
        }
    return ""


def report_disclosure_notice(context_data):
    """The escaped disclosure notice for a capped or sample-only output, or ``""``.

    Class-only markup by policy: inline ``style`` attributes are banned in
    Python emitters (CSP gate). The class is styled in the application
    stylesheet and in the polished standalone report template.
    """
    text = str(context_data.get("disclosure_text") or "").strip()
    if not text:
        return ""
    return f'<aside class="report-output-disclosure" data-report-disclosure="true">{escape(text)}</aside>'


_CUSTOM_CONTEXT_KEYS = frozenset(
    {
        "report_name",
        "description",
        "generated_at",
        "headers",
        "grouped_data",
        "summary_cards",
        "distribution_chart",
        "style_preset",
        "is_compact",
        "is_financial",
        "is_sample",
        "row_limit",
        "truncated",
        "total_rows",
        "disclosure_text",
        "request",
    }
)


def _safe_custom_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _safe_custom_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_custom_value(item) for item in value]
    return None


def _custom_context(context_data):
    return {key: _safe_custom_value(context_data[key]) for key in _CUSTOM_CONTEXT_KEYS if key in context_data}


_ALLOWED_ELEMENTS = frozenset(
    "a abbr b blockquote br caption code col colgroup dd div dl dt em figcaption figure h1 h2 h3 h4 h5 h6 hr i img"
    " li ol p pre s small span strong sub sup table tbody td tfoot th thead tr u ul".split()
)
# Subtrees removed together with their text: executable, framing, form and
# foreign-content elements whose inner text would otherwise leak into the page.
_DROP_ELEMENTS = frozenset(
    "applet base button embed form frameset iframe input link math meta noembed noframes noscript object"
    " option script select style svg template textarea title video audio".split()
)
_GLOBAL_ATTRIBUTES = frozenset({"aria-label", "class", "dir", "lang", "title"})
_ELEMENT_ATTRIBUTES = {
    "a": frozenset({"href"}),
    "img": frozenset({"alt", "height", "src", "width"}),
    "col": frozenset({"span"}),
    "td": frozenset({"align", "colspan", "rowspan", "valign"}),
    "th": frozenset({"align", "colspan", "rowspan", "scope", "valign"}),
}
_LINK_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
_IMAGE_SCHEMES = frozenset({"http", "https"})
_SAFE_DATA_IMAGE = re.compile(r"^data:image/(?:gif|jpe?g|png);base64,[A-Za-z0-9+/=]+$", re.IGNORECASE)
_URL_NOISE = re.compile(r"[\x00-\x20\x7f-\xa0]+")


def _safe_url(value, schemes, allow_data_image=False):
    """Accept relative, fragment and allowlisted-scheme URLs; reject everything else."""
    cleaned = _URL_NOISE.sub("", value or "")
    if not cleaned:
        return False
    if allow_data_image and _SAFE_DATA_IMAGE.fullmatch(cleaned):
        return True
    try:
        scheme = urlsplit(cleaned).scheme.lower()
    except ValueError:
        return False
    return not scheme or scheme in schemes


def _sanitize_attributes(element, tag):
    allowed = _GLOBAL_ATTRIBUTES | _ELEMENT_ATTRIBUTES.get(tag, frozenset())
    for raw_name, raw_value in list(element.attrib.items()):
        name = raw_name.rsplit("}", 1)[-1].lower()
        value = str(raw_value)
        keep = name in allowed and not name.startswith("on")
        if keep and name == "href":
            keep = _safe_url(value, _LINK_SCHEMES)
        elif keep and name == "src":
            keep = _safe_url(value, _IMAGE_SCHEMES, allow_data_image=True)
        if not keep:
            del element.attrib[raw_name]


def _splice(parent, child, replacement):
    """Replace ``child`` with ``replacement`` nodes, keeping text and tail in order."""
    index = list(parent).index(child)
    tail = child.tail or ""
    parent.remove(child)
    previous = parent[index - 1] if index else None
    for offset, node in enumerate(replacement):
        parent.insert(index + offset, node)
    last = replacement[-1] if replacement else previous
    if last is None:
        parent.text = (parent.text or "") + tail
    elif last is previous or last in replacement:
        last.tail = (last.tail or "") + tail


def _sanitize_children(parent):
    for child in list(parent):
        tag = child.tag.rsplit("}", 1)[-1].lower() if isinstance(child.tag, str) else None
        if tag is None or tag in _DROP_ELEMENTS:
            _splice(parent, child, [])
        elif tag in _ALLOWED_ELEMENTS:
            _sanitize_attributes(child, tag)
            _sanitize_children(child)
        else:
            # Unknown element: drop the tag, keep its (sanitised) content.
            _sanitize_children(child)
            lead = child.text or ""
            kept = list(child)
            if lead:
                index = list(parent).index(child)
                if index:
                    parent[index - 1].tail = (parent[index - 1].tail or "") + lead
                else:
                    parent.text = (parent.text or "") + lead
            _splice(parent, child, kept)


def sanitize_report_html(html):
    """Return ``html`` reduced to an allowlist of presentational markup.

    Autoescape protects interpolated values only; literal markup in a stored
    template reaches the page and the mailbox unchanged.  The output is parsed
    with html5lib and rebuilt from an element and attribute allowlist: script,
    style, frame, form and foreign-content subtrees are removed, event-handler
    and style attributes are dropped, and URLs are limited to http(s), mailto,
    tel, relative and inline raster images.  It applies regardless of any
    Content-Security-Policy, so a proxy that strips the header does not turn a
    stored template into script execution, and email has no CSP at all.
    """
    # inline import: heavy-import: html5lib is needed only when rendering custom HTML reports
    import html5lib

    fragment = html5lib.parseFragment(html or "", treebuilder="etree", namespaceHTMLElements=False)
    _sanitize_children(fragment)
    return html5lib.serialize(fragment, tree="etree", quote_attr_values="always", omit_optional_tags=False)


def render_report_html(context_data, template=None):
    """Render custom sandboxed HTML when present, otherwise the curated HTML.

    Custom HTML executes through the hardened sandbox whenever template content
    is stored, and the result is sanitised before it is returned to any sink
    (preview, download, PDF, scheduled mail).  A capped window or sample-only
    output appends the disclosure notice to the custom output as well:
    forwarding the artifact must not strip the qualifier.  The notice is
    generated, escaped markup and is appended after sanitising.
    """
    template_content = (getattr(template, "template_content", "") or "").strip()
    if template_content:
        env = hardened_sandbox_environment(autoescape=True)
        rendered = sanitize_report_html(env.from_string(template_content).render(_custom_context(context_data)))
        notice = report_disclosure_notice(context_data)
        return f"{rendered}\n{notice}" if notice else rendered
    return Template(get_polished_system_html_template()).render(Context(context_data))


def _append_csv_disclosure(writer, disclosure_text):
    """Append the truncation/sample disclosure as a final, separated row."""
    if not disclosure_text:
        return
    writer.writerow([])
    writer.writerow([csv_safe(disclosure_text)])


def render_report_csv(headers, rows, disclosure_text=""):
    """Render the canonical columns and append any sample/truncation disclosure.

    ``disclosure_text`` is appended as a clearly separated trailer row when
    the compiled window was capped or the report shows its sample, so the
    file itself states that it is not the full population.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([csv_safe(row.get(header, "-")) for header in headers])
    _append_csv_disclosure(writer, disclosure_text)
    return buffer.getvalue()

"""One label engine for every labelled model.

The single-object and the batch print paths, synchronous and queued, all go
through :func:`render_labels_pdf` / :func:`render_label_cards`, so a label looks
the same however it was requested. What a label says comes from the model's
registration in :mod:`itambox.labels`; nothing here names a domain model.
"""

import base64
import io
import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import TypeVar

from django.utils.html import escape, format_html
from django.utils.translation import gettext_lazy as _

from core.html_sanitizer import sanitize_label_html_for_pdf
from core.pdf_renderer import html_to_pdf_bytes
from extras.models import LabelTemplate
from itambox.labels import LabelSpec, label_reference, label_registry

logger = logging.getLogger(__name__)

_LABEL_PRINT_CSS_PATH = Path(__file__).resolve().parents[2] / "static" / "src" / "styles" / "_label-print.scss"

LAYOUT_ROLL = "roll"
LAYOUT_A4_GRID = "a4_grid"
LAYOUT_LETTER_GRID = "letter_grid"
LAYOUT_CHOICES = (
    (LAYOUT_ROLL, _("Continuous roll (one label per page)")),
    (LAYOUT_A4_GRID, _("A4 sheet (3 x 8 grid)")),
    (LAYOUT_LETTER_GRID, _("US Letter sheet (3 x 8 grid)")),
)
LAYOUTS = frozenset(value for value, _label in LAYOUT_CHOICES)

MAX_TEMPLATE_SOURCE = 64 * 1024
MAX_RENDERED_CARD = 256 * 1024

_ChunkItem = TypeVar("_ChunkItem")


class LabelNotApplicable(ValueError):
    """The template cannot print labels for this object (wrong type or format)."""


@dataclass(frozen=True)
class LabelContent:
    """Everything a label may show for one object, as plain strings."""

    title: str
    values: dict[str, str]
    field_labels: dict[str, str]
    reference: str
    linear_value: str | None
    template_alias: str | None
    model_name: str


def label_spec_for(obj) -> LabelSpec:
    spec = label_registry.get(type(obj))
    if spec is None:
        raise LabelNotApplicable(f"{obj._meta.label} carries no label")
    return spec


def label_content(obj) -> LabelContent:
    spec = label_spec_for(obj)
    linear = spec.barcode_value(obj) if spec.barcode_value is not None else None
    return LabelContent(
        title=str(spec.title(obj) or ""),
        values=spec.values(obj),
        field_labels={label_field.key: str(label_field.label) for label_field in spec.fields},
        reference=label_reference(obj),
        linear_value=str(linear) if linear not in (None, "") else None,
        template_alias=spec.template_alias,
        model_name=str(obj._meta.verbose_name),
    )


def default_label_template() -> LabelTemplate:
    """The built-in template used when no saved template targets the object type."""
    return LabelTemplate(
        name=str(_("Built-in label")),
        page_width=2.25,
        page_height=1.25,
        barcode_format=LabelTemplate.FORMAT_QR,
        template_code="",
    )


def template_applies(template: LabelTemplate, model) -> bool:
    """Whether ``template`` can print labels for ``model``.

    A saved template must list the model's type; the built-in template applies
    to every labelled model. Linear formats need the model's short identifier.
    """
    spec = label_registry.get(model)
    if spec is None:
        return False
    if template.uses_linear_barcode and not spec.supports_linear_barcodes:
        return False
    if template.pk is None:
        return True
    return LabelTemplate.for_model(model).filter(pk=template.pk).exists()


def templates_for(model) -> list[LabelTemplate]:
    """Saved templates usable for ``model``; the built-in template when there are none."""
    templates = [template for template in LabelTemplate.for_model(model) if template_applies(template, model)]
    return templates or [default_label_template()]


def barcode_data_uri(content: LabelContent, barcode_format: str) -> str:
    """Render the label's code as a PNG ``data:`` URI.

    QR encodes the object's globally unique reference. Linear formats encode
    the model's short identifier and refuse models without one, instead of
    silently printing something else.
    """
    buffer = io.BytesIO()
    if barcode_format == LabelTemplate.FORMAT_QR:
        # inline import: heavy-import: load the QR renderer only when generating graphics
        import segno

        # border=4 is the mandatory QR quiet zone; without it the code does not scan.
        segno.make_qr(content.reference).save(buffer, kind="png", scale=6, border=4)
    elif barcode_format in LabelTemplate.LINEAR_FORMATS:
        if content.linear_value is None:
            raise LabelNotApplicable(f"{content.model_name} labels support QR codes only")
        # inline imports: heavy-import: load the barcode renderer only when generating graphics
        import barcode
        from barcode.writer import ImageWriter

        barcode.get_barcode_class(barcode_format)(content.linear_value, writer=ImageWriter()).write(buffer)
    else:
        raise LabelNotApplicable(f"unsupported barcode format {barcode_format!r}")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def default_label_card(content: LabelContent, data_uri: str) -> str:
    """The built-in card: title, the first two other non-empty fields, the code.

    Every interpolated value is escaped: the card is emitted with ``|safe``.
    """
    lines = []
    for key, value in content.values.items():
        if value and value != content.title:
            lines.append((key, value))
        if len(lines) == 2:
            break
    primary = escape(lines[0][1]) if lines else ""
    secondary = ""
    if len(lines) > 1:
        key, value = lines[1]
        secondary = f'<div class="label-card-serial">{escape(content.field_labels[key])}: {escape(value)}</div>'
    barcode_img = format_html('<img src="{}" class="label-card-barcode" />', data_uri)
    return f"""
    <table class="label-card-table">
        <tr>
            <td class="label-card-metadata">
                    <div class="label-card-title">{escape(content.title)}</div>
                    <div class="label-card-tag">{primary}</div>
                    {secondary}
            </td>
            <td class="label-card-barcode-cell">
                {barcode_img}
            </td>
        </tr>
    </table>
    """


def _template_context(content: LabelContent, template: LabelTemplate, data_uri: str) -> dict[str, object]:
    # inline import: heavy-import: markupsafe ships with Jinja2, needed only for custom templates
    from markupsafe import Markup

    obj = {"title": content.title, **content.values}
    context: dict[str, object] = {
        "obj": obj,
        "model_name": content.model_name,
        "reference": content.reference,
        "barcode_data_uri": data_uri,
        "barcode_img": Markup(str(format_html('<img src="{}" class="label-card-barcode" />', data_uri))),
        "barcode_format": template.barcode_format,
    }
    if content.template_alias:
        context[content.template_alias] = obj
    return context


def render_label_card(content: LabelContent, template: LabelTemplate, data_uri: str) -> str:
    """Render one card with the template's custom code, or the built-in card.

    Custom code runs in an immutable autoescaping sandbox over scalar strings
    only (never a model instance), and its output is sanitized for the PDF
    renderer. Invalid or oversized custom code degrades to the built-in card.
    """
    if not template.template_code:
        return default_label_card(content, data_uri)
    try:
        # inline imports: heavy-import: Jinja2 is needed only when rendering a custom label
        from jinja2 import StrictUndefined
        from jinja2.sandbox import ImmutableSandboxedEnvironment

        if len(template.template_code) > MAX_TEMPLATE_SOURCE:
            raise ValueError("label template exceeds the maximum source size")
        env = ImmutableSandboxedEnvironment(autoescape=True, undefined=StrictUndefined)
        for unsafe_filter in ("attr", "format", "format_map", "map", "pprint", "xmlattr"):
            env.filters.pop(unsafe_filter, None)
        for unsafe_global in ("cycler", "joiner", "namespace", "lipsum"):
            env.globals.pop(unsafe_global, None)
        rendered = env.from_string(template.template_code).render(**_template_context(content, template, data_uri))
        if len(rendered) > MAX_RENDERED_CARD:
            raise ValueError("rendered label exceeds the maximum output size")
        return sanitize_label_html_for_pdf(rendered, allowed_data_uris=frozenset({data_uri}))
    # broad except: render-degrade: invalid optional custom markup safely degrades to the built-in card
    except Exception as exc:
        logger.warning(
            "Custom label rendering degraded to the default layout",
            extra={
                "operation": "labels.custom_template",
                "template_id": getattr(template, "pk", None),
                "exception_type": type(exc).__name__,
            },
        )
        return default_label_card(content, data_uri)


def render_object_card(obj, template: LabelTemplate) -> str:
    """Render the card for one object; raises :class:`LabelNotApplicable` on a mismatch."""
    content = label_content(obj)
    return render_label_card(content, template, barcode_data_uri(content, template.barcode_format))


def chunk_list(items: Sequence[_ChunkItem], size: int) -> Iterator[Sequence[_ChunkItem]]:
    """Yield successive ``size``-sized chunks of ``items``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def label_print_css() -> str:
    """The authored label CSS, embedded into the self-contained PDF document."""
    return _LABEL_PRINT_CSS_PATH.read_text(encoding="utf-8")


def safe_label_measurement(value: object, default: float) -> str:
    """A bounded, CSS-safe inch measurement for a label dimension."""
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        numeric = default
    if not isfinite(numeric) or not 0.1 <= numeric <= 100:
        numeric = default
    return f"{numeric:.3f}".rstrip("0").rstrip(".")


def _grid_document(cards: Sequence[str], layout: str, css: str) -> str:
    paper_size = "a4" if layout == LAYOUT_A4_GRID else "letter"
    margin = "10mm" if layout == LAYOUT_A4_GRID else "0.5in"
    cell_height = "34mm" if layout == LAYOUT_A4_GRID else "1.22in"
    pages = list(chunk_list(list(cards), 24))
    pages_block = ""
    for page_index, page_cards in enumerate(pages):
        padded: list[str | None] = list(page_cards)
        while len(padded) % 3:
            padded.append(None)
        page_break = "page-break-always" if page_index < len(pages) - 1 else "page-break-avoid"
        rows_block = ""
        for row in chunk_list(padded, 3):
            cells = "".join(f'<td class="grid-cell">{card if card else "&nbsp;"}</td>\n' for card in row)
            rows_block += f"<tr>\n{cells}</tr>\n"
        pages_block += f'<table class="grid-table {page_break}">\n{rows_block}</table>\n'
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        {css}
        @page {{
            size: {paper_size};
            margin: {margin};
        }}
        .grid-cell {{
            height: {cell_height};
        }}
    </style>
</head>
<body>
    {pages_block}
</body>
</html>"""


def build_labels_document(cards: Sequence[str], template: LabelTemplate, layout: str) -> str:
    """Wrap rendered cards into one printable HTML document for ``layout``.

    Unknown layouts fall back to the continuous roll, sized to the template.
    """
    css = label_print_css()
    if layout in (LAYOUT_A4_GRID, LAYOUT_LETTER_GRID):
        return _grid_document(cards, layout, css)
    width = safe_label_measurement(getattr(template, "page_width", None), 2.25)
    height = safe_label_measurement(getattr(template, "page_height", None), 1.25)
    cards_block = ""
    for index, card in enumerate(cards):
        page_break = "page-break-always" if index < len(cards) - 1 else "page-break-avoid"
        cards_block += f'<div class="label-card {page_break}">{card}</div>\n'
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        {css}
        @page {{
            size: {width}in {height}in;
            margin: 0;
        }}
        .label-card {{
            width: {width}in;
            height: {height}in;
        }}
    </style>
</head>
<body>
    {cards_block}
</body>
</html>"""


def render_labels_pdf(objects: Iterable[object], template: LabelTemplate, layout: str = LAYOUT_ROLL) -> bytes:
    """Render labels for ``objects`` into one PDF; every object must render.

    The synchronous path is all-or-nothing: an object the template cannot
    print raises instead of being dropped from a document the user will trust.
    """
    cards = [render_object_card(obj, template) for obj in objects]
    if not cards:
        raise ValueError("No labels were rendered.")
    return html_to_pdf_bytes(build_labels_document(cards, template, layout))

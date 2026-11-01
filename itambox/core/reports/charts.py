import math
import re
from pathlib import Path

from django.utils.html import escape, format_html
from django.utils.translation import gettext as _

from core.context import get_current_csp_nonce

_CHART_CSS_PATH = Path(__file__).resolve().parents[2] / "static" / "src" / "styles" / "_report-output.scss"
_SAFE_NONCE = re.compile(r"^[A-Za-z0-9+/_=-]{1,256}$")


def _chart_style_block():
    css = _CHART_CSS_PATH.read_text(encoding="utf-8")
    nonce = get_current_csp_nonce()
    if nonce and _SAFE_NONCE.fullmatch(nonce):
        return format_html('<style nonce="{}">{}</style>', nonce, css)
    return ""


def generate_doughnut_chart(data, title=""):
    """
    Generates a beautifully styled, self-contained SVG doughnut chart.
    data: list of dicts like [{'label': 'Deployed', 'value': 45}, ...]
    """
    # Filter out zero or negative values
    data = [item for item in data if item.get("value", 0) > 0]
    if not data:
        return "\n".join(
            [
                _chart_style_block(),
                '<svg class="report-chart report-chart-empty" viewBox="0 0 480 220" '
                'width="100%" height="220" xmlns="http://www.w3.org/2000/svg">',
                '<rect width="480" height="220" rx="12" fill="#f8fafc" stroke="#e2e8f0" stroke-width="1"/>',
                f'<text x="240" y="115" text-anchor="middle" font-size="13" fill="#64748b" '
                f'font-weight="500">{_("No data available for charting")}</text>',
                "</svg>",
            ]
        )

    total = sum(item["value"] for item in data)
    r = 60
    circumference = 2 * math.pi * r  # ~376.99

    colors = [
        "#206bc4",
        "#4263eb",
        "#2fb344",
        "#f76707",
        "#0ca678",
        "#ae3ec9",
        "#d63939",
        "#f59f00",
        "#74b816",
        "#66d9e8",
    ]

    svg_parts = []
    svg_parts.append(_chart_style_block())
    svg_parts.append(
        '<svg class="report-chart report-chart-doughnut" viewBox="0 0 480 220" '
        'width="100%" height="220" xmlns="http://www.w3.org/2000/svg">'
    )

    if title:
        svg_parts.append(
            f'<text x="20" y="25" font-size="13" font-weight="700" fill="#0f172a" letter-spacing="-0.2px">{escape(title)}</text>'
        )

    # Doughnut circle base
    svg_parts.append('<g transform="rotate(-90 110 120)">')

    accumulated_percent = 0
    for idx, item in enumerate(data):
        val = item["value"]
        pct = val / total
        color = colors[idx % len(colors)]

        stroke_dasharray = f"{pct * circumference:.2f} {circumference:.2f}"
        stroke_dashoffset = f"{-accumulated_percent * circumference:.2f}"

        svg_parts.append(
            '<circle cx="110" cy="120" r="{}" fill="transparent" '
            'stroke="{}" stroke-width="16" '
            'stroke-dasharray="{}" stroke-dashoffset="{}" />'.format(r, color, stroke_dasharray, stroke_dashoffset)
        )
        accumulated_percent += pct

    svg_parts.append("</g>")

    # Doughnut Center Cutout and Text (Total count)
    svg_parts.append('<circle cx="110" cy="120" r="{}" fill="#ffffff" />'.format(r - 8))
    svg_parts.append(
        f'<text x="110" y="118" text-anchor="middle" font-size="18" font-weight="800" fill="#0f172a">{total:,}</text>'
    )
    svg_parts.append(
        f'<text x="110" y="132" text-anchor="middle" font-size="9" font-weight="600" fill="#64748b" '
        f'text-transform="uppercase" letter-spacing="0.5px">{_("Total")}</text>'
    )

    # Legend Section (right side, starting at x=230)
    legend_y_start = 45
    legend_row_height = 25

    # Show at most 6 items in the legend, group remainder under "Other"
    visible_items = data[:6]
    other_items = data[6:]

    if other_items:
        other_sum = sum(item["value"] for item in other_items)
        visible_items.append({"label": _("Other"), "value": other_sum})

    for idx, item in enumerate(visible_items):
        y_pos = legend_y_start + (idx * legend_row_height)
        color = "#64748b" if item["label"] == _("Other") or item["label"] == "Other" else colors[idx % len(colors)]
        pct_str = f"{(item['value'] / total) * 100:.1f}%"

        # Color indicator circle
        svg_parts.append('<circle cx="235" cy="{}" r="5" fill="{}" />'.format(y_pos - 4, color))
        # Label text — escape user-controlled category/status/provider names
        svg_parts.append(
            '<text x="250" y="{}" font-size="11" font-weight="500" fill="#334155">{}</text>'.format(
                y_pos, escape(item["label"])
            )
        )
        # Value text (right-aligned at x=390) — numeric, safe as-is
        svg_parts.append(
            '<text x="390" y="{}" font-size="11" font-weight="700" fill="#0f172a" text-anchor="end">{:,}</text>'.format(
                y_pos, item["value"]
            )
        )
        # Percentage text (right-aligned at x=450) — computed float string, safe as-is
        svg_parts.append(
            '<text x="450" y="{}" font-size="10" font-weight="600" fill="#64748b" text-anchor="end">{}</text>'.format(
                y_pos, pct_str
            )
        )

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)


def generate_bar_chart(data, title=""):
    """
    Generates a beautifully styled horizontal SVG bar chart.
    data: list of dicts like [{'label': 'Office 365', 'value': 1200.00}, ...]
    """
    # Filter out zero or negative values
    data = [item for item in data if item.get("value", 0) > 0]
    if not data:
        return "\n".join(
            [
                _chart_style_block(),
                '<svg class="report-chart report-chart-empty" viewBox="0 0 480 220" '
                'width="100%" height="220" xmlns="http://www.w3.org/2000/svg">',
                '<rect width="480" height="220" rx="12" fill="#f8fafc" stroke="#e2e8f0" stroke-width="1"/>',
                f'<text x="240" y="115" text-anchor="middle" font-size="13" fill="#64748b" '
                f'font-weight="500">{_("No data available for charting")}</text>',
                "</svg>",
            ]
        )

    # Keep top 5 items
    data = sorted(data, key=lambda x: x["value"], reverse=True)[:5]
    max_val = max(item["value"] for item in data)

    colors = ["#206bc4", "#4263eb", "#2fb344", "#f76707", "#0ca678"]

    svg_parts = []
    svg_parts.append(_chart_style_block())
    svg_parts.append(
        '<svg class="report-chart report-chart-bar" viewBox="0 0 480 220" '
        'width="100%" height="220" xmlns="http://www.w3.org/2000/svg">'
    )

    if title:
        svg_parts.append(
            f'<text x="20" y="25" font-size="13" font-weight="700" fill="#0f172a" letter-spacing="-0.2px">{escape(title)}</text>'
        )

    y_start = 50
    row_height = 32
    max_bar_width = 240

    for idx, item in enumerate(data):
        val = item["value"]
        label = item["label"]
        color = colors[idx % len(colors)]

        # Calculate bar width proportion
        bar_width = max(int((val / max_val) * max_bar_width), 8)
        y_pos = y_start + (idx * row_height)

        # Truncate label if too long, then escape — label is a user-controlled provider name
        short_label = label if len(label) <= 18 else label[:16] + "..."

        # Label text
        svg_parts.append(
            '<text x="20" y="{}" font-size="11" font-weight="600" fill="#475569" text-anchor="start">{}</text>'.format(
                y_pos + 12, escape(short_label)
            )
        )

        # Bar background
        svg_parts.append(
            '<rect x="140" y="{}" width="{}" height="16" rx="4" fill="#f1f5f9" />'.format(y_pos, max_bar_width)
        )

        # Actual colored bar
        svg_parts.append(
            '<rect x="140" y="{}" width="{}" height="16" rx="4" fill="{}" />'.format(y_pos, bar_width, color)
        )

        # Value label (right of bar). Prefer a caller-supplied per-currency display
        # string (the compiler formats money via the money templatetag); fall back to
        # a plain number for count charts — never hardcode a '$'.
        if item.get("display"):
            val_str = str(item["display"])
        elif isinstance(val, float):
            val_str = f"{val:,.2f}"
        else:
            val_str = f"{int(val):,}"
        svg_parts.append(
            '<text x="{}" y="{}" font-size="11" font-weight="700" fill="#0f172a" text-anchor="start">{}</text>'.format(
                145 + bar_width,
                y_pos + 12,
                escape(val_str),
            )
        )

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)

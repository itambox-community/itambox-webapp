"""Report output exporters.

XLSX via openpyxl; PDF via the shared xhtml2pdf renderer already used for label
PDFs (``assets.tasks.labels._html_to_pdf_bytes``), which carries an SSRF-safe link
callback so user-authored report templates can't fetch remote/internal resources.
Both take the already-compiled grid (headers + rows keyed by translated header)
or rendered HTML, so they stay format-agnostic across report types.
"""

import io

from core.pdf_renderer import html_to_pdf_bytes

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MIME = "application/pdf"


def report_xlsx_bytes(headers, rows, sheet_title="Report", disclosure_text=""):
    """Render the report grid (headers + rows) into an .xlsx workbook (bytes).

    ``disclosure_text`` is written as an italic note row below the grid when
    the compiled window was capped or the report shows its sample.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    # Excel caps sheet names at 31 chars and forbids []:*?/\
    safe_title = "".join(c for c in (sheet_title or "Report") if c not in "[]:*?/\\")[:31] or "Report"
    ws.title = safe_title

    ws.append(list(headers))
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for r in rows:
        ws.append([r.get(h, "-") for h in headers])

    if disclosure_text:
        note = ws.cell(row=ws.max_row + 2, column=1, value=disclosure_text)
        note.font = Font(italic=True)

    # Report strings can be user-controlled; openpyxl otherwise treats leading "=" as a formula.
    # Keep headings, values, and the disclosure note as literal spreadsheet text.
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = "s"

    for col_idx, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = max(12, min(40, len(str(h)) + 4))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def report_pdf_bytes(rendered_html):
    """Render already-compiled report HTML into PDF bytes via the shared
    xhtml2pdf renderer (same engine + SSRF-safe link callback as label PDFs)."""
    return html_to_pdf_bytes(rendered_html)

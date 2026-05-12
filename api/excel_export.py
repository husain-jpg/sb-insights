"""
Excel report generator.

One generic writer used by every tab's export endpoint. Produces a formatted
.xlsx workbook with frozen header row, auto-sized columns, bold header,
currency/number formatting, and optional totals row.

All exports reuse this so formatting stays consistent.
"""
from __future__ import annotations

from io import BytesIO
from typing import Any, Callable, Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter


# Visual tokens — kept restrained so spreadsheets look professional, not gaudy.
HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
TOTALS_FONT = Font(bold=True, size=11)
TOTALS_FILL = PatternFill(start_color="F3F4F6", end_color="F3F4F6", fill_type="solid")
THIN_BORDER = Border(bottom=Side(style="thin", color="E5E7EB"))


def _auto_column_width(ws, n_cols: int, max_rows_to_check: int = 200) -> None:
    """Size columns to fit the wider of header and first N rows of data."""
    for col_idx in range(1, n_cols + 1):
        letter = get_column_letter(col_idx)
        max_len = 0
        for row in range(1, min(ws.max_row, max_rows_to_check) + 1):
            cell = ws.cell(row=row, column=col_idx)
            v = cell.value
            if v is None:
                continue
            txt = str(v)
            if len(txt) > max_len:
                max_len = len(txt)
        # Add a little padding; cap so huge product names don't blow out layout
        ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 55)


def build_workbook(
    *,
    sheet_name: str,
    title: str,
    subtitle: str | None,
    columns: list[dict],
    rows: Iterable[dict],
    totals: dict[str, Any] | None = None,
) -> bytes:
    """
    Build an Excel workbook as bytes.

    `columns` = list of dicts: {
        "key": "field_name",
        "label": "Header Text",
        "format": "text" | "int" | "currency" | "decimal" | "percent",   # optional
        "width": int                                                        # optional
    }

    `rows` = iterable of dicts; values looked up by column key.
    `totals` = optional dict of {column_key: value} for a totals row.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]  # Excel sheet name length limit

    # Title block — rows 1 and 2
    ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=14)
    if subtitle:
        ws.cell(row=2, column=1, value=subtitle).font = Font(italic=True, color="6B7280", size=10)

    # Header row (row 4)
    header_row = 4
    for i, col in enumerate(columns, start=1):
        cell = ws.cell(row=header_row, column=i, value=col["label"])
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = THIN_BORDER

    # Data rows
    row_idx = header_row + 1
    rows_list = list(rows)
    for record in rows_list:
        for i, col in enumerate(columns, start=1):
            val = record.get(col["key"])
            cell = ws.cell(row=row_idx, column=i, value=val)
            fmt = col.get("format", "text")
            if fmt == "currency" and isinstance(val, (int, float)):
                cell.number_format = '"$"#,##0.00'
                cell.alignment = Alignment(horizontal="right")
            elif fmt == "int" and isinstance(val, (int, float)):
                cell.number_format = "#,##0"
                cell.alignment = Alignment(horizontal="right")
            elif fmt == "decimal" and isinstance(val, (int, float)):
                cell.number_format = "0.00"
                cell.alignment = Alignment(horizontal="right")
            elif fmt == "percent" and isinstance(val, (int, float)):
                cell.number_format = "0.0%"
                cell.alignment = Alignment(horizontal="right")
        row_idx += 1

    # Totals row
    if totals:
        for i, col in enumerate(columns, start=1):
            key = col["key"]
            val = totals.get(key)
            cell = ws.cell(row=row_idx, column=i, value=val if val is not None else ("Total:" if i == 1 else ""))
            cell.font = TOTALS_FONT
            cell.fill = TOTALS_FILL
            cell.border = Border(top=Side(style="medium"))
            fmt = col.get("format", "text")
            if val is not None and fmt == "currency" and isinstance(val, (int, float)):
                cell.number_format = '"$"#,##0.00'
                cell.alignment = Alignment(horizontal="right")
            elif val is not None and fmt == "int" and isinstance(val, (int, float)):
                cell.number_format = "#,##0"
                cell.alignment = Alignment(horizontal="right")
        row_idx += 1

    # Freeze panes below header so it stays visible when scrolling
    ws.freeze_panes = f"A{header_row + 1}"

    # Auto-size columns
    _auto_column_width(ws, n_cols=len(columns))

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()

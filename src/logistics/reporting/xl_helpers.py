"""openpyxl helpers implementing the portfolio workbook design language.

Design language (shared with the Dash and static dashboards via src.shared.style):
ink title band, teal section rules, mist KPI cards with a teal accent edge,
Arial throughout, blue font for editable inputs, black for formulas, green for
cross-sheet links.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pandas as pd
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.formatting.rule import ColorScaleRule, DataBarRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.worksheet import Worksheet

from src.shared.style import FONT_EXCEL, PALETTE, SERIES

F = FONT_EXCEL
INK, TEAL, SLATE, MARIGOLD, SIGNAL, LEAF, MIST, RULE = (PALETTE[k] for k in
                                                        ("ink", "teal", "slate", "marigold", "signal", "leaf", "mist", "rule"))
THIN = Side(style="thin", color=RULE)
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
FMT = {"inr": '"₹"#,##0;("₹"#,##0);"-"', "inr2": '"₹"#,##0.00', "cr": '"₹"0.00" cr";("₹"0.00" cr")', "pct": "0.0%",
       "pct_pts": '0.0" pts"', "int": "#,##0", "dec1": "#,##0.0", "dec2": "#,##0.00", "dec3": "0.000", "x": '0.00"x"',
       "min": '0.0" min"', "km": '#,##0.0" km"'}
_TABLE_IDS: set[str] = set()


def font(size: float = 10, bold: bool = False, color: str = "000000", italic: bool = False) -> Font:
    return Font(name=F, size=size, bold=bold, color=color, italic=italic)


def setup_sheet(ws: Worksheet, title: str, subtitle: str, width_cols: int = 16, tab: str = TEAL) -> None:
    """Title band, subtitle, gridlines off, default column widths."""
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = tab
    for c in range(1, width_cols + 1):
        ws.column_dimensions[get_column_letter(c)].width = 14
    ws.column_dimensions["A"].width = 2
    ws.merge_cells(start_row=1, start_column=2, end_row=1, end_column=width_cols)
    cell = ws.cell(1, 2, title)
    cell.font = font(18, True, "FFFFFF")
    cell.alignment = Alignment(vertical="center", indent=1)
    for c in range(1, width_cols + 1):
        ws.cell(1, c).fill = PatternFill("solid", fgColor=INK)
    ws.row_dimensions[1].height = 34
    ws.cell(2, 2, subtitle).font = font(10, italic=True, color=SLATE)
    ws.row_dimensions[2].height = 18


def section(ws: Worksheet, row: int, col: int, text: str, span: int = 8, note: str | None = None) -> None:
    """Section heading with a teal rule underneath."""
    ws.cell(row, col, text).font = font(12, True, TEAL)
    for c in range(col, col + span):
        ws.cell(row, c).border = Border(bottom=Side(style="medium", color=TEAL))
    if note:
        ws.cell(row + 1, col, note).font = font(9, italic=True, color=SLATE)


def kpi_card(ws: Worksheet, row: int, col: int, label: str, value: Any, fmt: str = "General",
             span: int = 2, accent: str = TEAL, sub: str | None = None) -> str:
    """Two-row KPI card; ``value`` may be a number or a formula. Returns the value cell address."""
    ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + span - 1)
    ws.merge_cells(start_row=row + 1, start_column=col, end_row=row + 1, end_column=col + span - 1)
    v = ws.cell(row, col, value)
    v.font = font(18, True, INK if not str(value).startswith("=") or True else "008000")
    v.number_format = fmt
    v.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    lab = ws.cell(row + 1, col, label if not sub else f"{label} · {sub}")
    lab.font = font(9, color=SLATE)
    lab.alignment = Alignment(horizontal="left", vertical="top", indent=1, wrap_text=True)
    for r in (row, row + 1):
        for c in range(col, col + span):
            ws.cell(r, c).fill = PatternFill("solid", fgColor=MIST)
        ws.cell(r, col).border = Border(left=Side(style="thick", color=accent))
    ws.row_dimensions[row].height = 30
    ws.row_dimensions[row + 1].height = 26
    return v.coordinate


def write_df(ws: Worksheet, df: pd.DataFrame, row: int, col: int, formats: dict[str, str] | None = None,
             table_name: str | None = None, widths: dict[str, float] | None = None, header_fill: str = INK,
             wrap_header: bool = True) -> tuple[int, int, int, int]:
    """Write a DataFrame as a styled, filterable Excel table. Returns (r0, r1, c0, c1)."""
    formats = formats or {}
    df = df.copy()
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            df[c] = df[c].dt.date
        if pd.api.types.is_bool_dtype(df[c]):
            df[c] = df[c].map({True: "Yes", False: "No"})
    for j, name in enumerate(df.columns):
        h = ws.cell(row, col + j, str(name))
        h.font = font(10, True, "FFFFFF")
        h.fill = PatternFill("solid", fgColor=header_fill)
        h.alignment = Alignment(horizontal="center", vertical="center", wrap_text=wrap_header)
        h.border = BOX
    ws.row_dimensions[row].height = 30
    for i, rec in enumerate(df.itertuples(index=False), start=1):
        for j, val in enumerate(rec):
            if isinstance(val, float) and pd.isna(val):
                val = None
            elif hasattr(val, "item"):
                val = val.item()
            c = ws.cell(row + i, col + j, val)
            c.font = font(10)
            c.border = BOX
            fmt = formats.get(df.columns[j])
            if fmt:
                c.number_format = FMT.get(fmt, fmt)
            if i % 2 == 0:
                c.fill = PatternFill("solid", fgColor="F7F9FB")
    r1, c1 = row + len(df), col + len(df.columns) - 1
    for j, name in enumerate(df.columns):
        letter = get_column_letter(col + j)
        w = (widths or {}).get(name)
        if w:
            ws.column_dimensions[letter].width = w
    if table_name and len(df):
        tid = re.sub(r"[^A-Za-z0-9_]", "_", table_name)
        base, k = tid, 1
        while tid in _TABLE_IDS:
            k += 1
            tid = f"{base}_{k}"
        _TABLE_IDS.add(tid)
        t = Table(displayName=tid, ref=f"{get_column_letter(col)}{row}:{get_column_letter(c1)}{r1}")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=False)
        ws.add_table(t)
    return row, r1, col, c1


def col_range(r0: int, r1: int, c: int) -> str:
    L = get_column_letter(c)
    return f"{L}{r0}:{L}{r1}"


def data_bar(ws: Worksheet, rng: str, color: str = TEAL) -> None:
    ws.conditional_formatting.add(rng, DataBarRule(start_type="min", end_type="max", color=color, showValue=True))


def color_scale(ws: Worksheet, rng: str, reverse: bool = False, mid: bool = True) -> None:
    lo, hi = ("F8D9D6", "D7EEE0") if not reverse else ("D7EEE0", "F8D9D6")
    if mid:
        ws.conditional_formatting.add(rng, ColorScaleRule(start_type="min", start_color=lo, mid_type="percentile",
                                                          mid_value=50, mid_color="FFF6DD", end_type="max", end_color=hi))
    else:
        ws.conditional_formatting.add(rng, ColorScaleRule(start_type="min", start_color="FFFFFF", end_type="max", end_color=hi))


def heat_scale(ws: Worksheet, rng: str) -> None:
    """White -> marigold -> ink heat scale for intensity grids."""
    ws.conditional_formatting.add(rng, ColorScaleRule(start_type="min", start_color="FFFFFF", mid_type="percentile",
                                                      mid_value=70, mid_color="F2C14E", end_type="max", end_color="1F2A44"))


def _style_chart(ch: Any, title: str, y_title: str | None, x_title: str | None, w: float, h: float) -> None:
    ch.title = title
    ch.style = 2
    ch.width, ch.height = w, h
    if y_title:
        ch.y_axis.title = y_title
    if x_title:
        ch.x_axis.title = x_title
    ch.legend.position = "b"
    for ax in (ch.x_axis, ch.y_axis):
        ax.delete = False


def bar_chart(ws_anchor: Worksheet, anchor: str, data_ws: Worksheet, cats: tuple[int, int, int],
              series_cols: list[int], header_row: int, title: str, y_title: str | None = None,
              x_title: str | None = None, horizontal: bool = False, stacked: bool = False,
              colors: list[str] | None = None, w: float = 16, h: float = 7.5, number_format: str | None = None) -> BarChart:
    """Clustered/stacked bar chart from columns of a written table (cats = (col, r0, r1))."""
    ch = BarChart()
    ch.type = "bar" if horizontal else "col"
    if stacked:
        ch.grouping = "stacked"
        ch.overlap = 100
    c, r0, r1 = cats
    for k, sc in enumerate(series_cols):
        ref = Reference(data_ws, min_col=sc, min_row=header_row, max_row=r1)
        ch.add_data(ref, titles_from_data=True)
        s = ch.series[-1]
        col = (colors or SERIES)[k % len(colors or SERIES)]
        s.graphicalProperties.solidFill = col
        s.graphicalProperties.line.solidFill = col
    ch.set_categories(Reference(data_ws, min_col=c, min_row=r0, max_row=r1))
    if number_format:
        ch.y_axis.numFmt = number_format
    _style_chart(ch, title, y_title, x_title, w, h)
    ws_anchor.add_chart(ch, anchor)
    return ch


def line_chart(ws_anchor: Worksheet, anchor: str, data_ws: Worksheet, cats: tuple[int, int, int],
               series_cols: list[int], header_row: int, title: str, y_title: str | None = None,
               x_title: str | None = None, colors: list[str] | None = None, w: float = 16, h: float = 7.5,
               dashed: set[int] | None = None, number_format: str | None = None, markers: bool = False) -> LineChart:
    ch = LineChart()
    c, r0, r1 = cats
    for k, sc in enumerate(series_cols):
        ch.add_data(Reference(data_ws, min_col=sc, min_row=header_row, max_row=r1), titles_from_data=True)
        s = ch.series[-1]
        col = (colors or SERIES)[k % len(colors or SERIES)]
        s.graphicalProperties.line.solidFill = col
        s.graphicalProperties.line.width = 22000
        s.smooth = False
        if dashed and k in dashed:
            s.graphicalProperties.line.dashStyle = "dash"
        if markers:
            s.marker.symbol = "circle"
            s.marker.size = 5
            s.marker.graphicalProperties.solidFill = col
            s.marker.graphicalProperties.line.solidFill = col
        else:
            s.marker.symbol = "none"
    ch.set_categories(Reference(data_ws, min_col=c, min_row=r0, max_row=r1))
    if number_format:
        ch.y_axis.numFmt = number_format
    _style_chart(ch, title, y_title, x_title, w, h)
    ws_anchor.add_chart(ch, anchor)
    return ch


def note(ws: Worksheet, row: int, col: int, text: str, span: int = 10) -> None:
    ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + span - 1)
    c = ws.cell(row, col, text)
    c.font = font(9, italic=True, color=SLATE)
    c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[row].height = max(15, 13 * (1 + len(text) // (span * 13)))


# =========================================================================== sparklines
def inject_sparklines(path: Path, sheet_index: dict[str, int], specs: dict[str, list[tuple[str, str]]]) -> int:
    """Add native Excel sparklines (x14 extension) to a saved workbook.

    ``specs`` maps sheet title -> [(data_range "'Sheet'!B5:CM5", target_cell "CN5"), ...].
    openpyxl cannot write sparklines, so the worksheet XML is extended in place.
    Returns the number of sparklines written.
    """
    tmp = Path(tempfile.mkdtemp()) / "wb.xlsx"
    shutil.copy(path, tmp)
    written = 0
    with zipfile.ZipFile(tmp) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            m = re.match(r"xl/worksheets/sheet(\d+)\.xml$", item.filename)
            title = next((t for t, i in sheet_index.items() if m and int(m.group(1)) == i), None)
            if title and specs.get(title):
                xml = data.decode("utf-8")
                lines = "".join(f"<x14:sparkline><xm:f>{escape(f)}</xm:f><xm:sqref>{t}</xm:sqref></x14:sparkline>"
                                for f, t in specs[title])
                group = (
                    '<ext uri="{05C60535-1F16-4fd2-B633-F4F36F0B64E0}" '
                    'xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main">'
                    '<x14:sparklineGroups xmlns:xm="http://schemas.microsoft.com/office/excel/2006/main">'
                    '<x14:sparklineGroup displayEmptyCellsAs="gap" high="1" lineWeight="1.25">'
                    f'<x14:colorSeries rgb="FF{TEAL}"/><x14:colorNegative rgb="FF{SIGNAL}"/>'
                    '<x14:colorAxis rgb="FF000000"/><x14:colorMarkers rgb="FF0E7C86"/>'
                    f'<x14:colorFirst rgb="FF{SLATE}"/><x14:colorLast rgb="FF{INK}"/>'
                    f'<x14:colorHigh rgb="FF{MARIGOLD}"/><x14:colorLow rgb="FF{SLATE}"/>'
                    f'<x14:sparklines>{lines}</x14:sparklines></x14:sparklineGroup></x14:sparklineGroups></ext>')
                if "<extLst>" in xml:
                    xml = xml.replace("</extLst>", group + "</extLst>")
                else:
                    xml = xml.replace("</worksheet>", f"<extLst>{group}</extLst></worksheet>")
                data = xml.encode("utf-8")
                written += len(specs[title])
            zout.writestr(item, data)
    return written

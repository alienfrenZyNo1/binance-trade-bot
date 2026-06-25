"""Telegram HTML formatting helpers.

These helpers are intentionally pure and lightweight: no Binance, database,
Telegram network, Socket.IO, or Eventlet imports. Telegram companion commands
can import them safely for HTML parse_mode output and snapshot-style tests.
"""

from __future__ import annotations

import html
from typing import Iterable, Sequence


def html_escape(text):
    """HTML-escape text for safe insertion into Telegram HTML messages.

    Telegram's HTML parse_mode requires <, >, & to be escaped in text content
    (including inside <pre>/<code> blocks). Quotes are left untouched to match
    the legacy Telegram companion output.
    """
    return html.escape(str(text), quote=False)


def format_table(headers, rows, aligns=None):
    """Build a clean, aligned monospace table string.

    Args:
        headers: list of column header strings.
        rows: list of lists; each inner list is one data row of cell values
            (numbers are coerced to str automatically).
        aligns: optional list of per-column alignment specifiers, one per
            column. Accepted values:
              'l'  – left-justify  (default; text columns)
              'r'  – right-justify (numeric columns; aligns by last char)
              'd'  – decimal-align (splits on last '.', pads integer part
                     left and fractional part right so decimal points line up)

    Returns:
        A multi-line string with a header row, a '──' separator line, and
        aligned data rows. The result is NOT html-escaped; callers can use
        pre_table() for the escaped Telegram-ready fragment.
    """
    ncols = len(headers)
    if aligns is None:
        aligns = ["l"] * ncols
    else:
        aligns = list(aligns)
    while len(aligns) < ncols:
        aligns.append("l")

    str_rows = []
    for row in rows:
        cells = [str(c) for c in row]
        while len(cells) < ncols:
            cells.append("")
        str_rows.append(cells[:ncols])

    for col_idx in range(ncols):
        if aligns[col_idx] != "d":
            continue
        lefts = []
        rights = []
        has_dot = []
        for row in str_rows:
            cell = row[col_idx]
            if "." in cell:
                idx = cell.rfind(".")
                lefts.append(cell[:idx])
                rights.append(cell[idx + 1:])
                has_dot.append(True)
            else:
                lefts.append(cell)
                rights.append("")
                has_dot.append(False)

        max_left = max((len(left) for left in lefts), default=0)
        max_right = max((len(right) for right in rights if right), default=0)

        for row_idx in range(len(str_rows)):
            left = lefts[row_idx].rjust(max_left)
            if has_dot[row_idx] and rights[row_idx]:
                right = rights[row_idx].ljust(max_right)
                str_rows[row_idx][col_idx] = left + "." + right
            elif has_dot[row_idx]:
                str_rows[row_idx][col_idx] = left + "." + " " * max_right
            else:
                str_rows[row_idx][col_idx] = left + " " * (max_right + 1 if max_right > 0 else 0)

    widths = [len(h) for h in headers]
    for row in str_rows:
        for i in range(ncols):
            widths[i] = max(widths[i], len(row[i]))

    sep = "  "
    header_cells = []
    for i in range(ncols):
        if aligns[i] in ("r", "d"):
            header_cells.append(headers[i].rjust(widths[i]))
        else:
            header_cells.append(headers[i].ljust(widths[i]))
    header_line = sep.join(header_cells)
    divider_line = sep.join("\u2500" * widths[i] for i in range(ncols))

    data_lines = []
    for row in str_rows:
        cells = []
        for i in range(ncols):
            if aligns[i] in ("r", "d"):
                cells.append(row[i].rjust(widths[i]))
            else:
                cells.append(row[i].ljust(widths[i]))
        data_lines.append(sep.join(cells))
    return "\n".join([header_line, divider_line] + data_lines)


def _annotate_pnl_emoji(table, pnl_values):
    """Prefix each data row of an aligned table with a 🟢/🔴 profit marker."""
    lines = table.split("\n")
    spacer = "   "
    out = []
    for i, line in enumerate(lines):
        if i < 2:
            out.append(spacer + line)
        else:
            idx = i - 2
            if idx < len(pnl_values):
                val = pnl_values[idx]
                if val is None:
                    out.append(spacer + line)
                else:
                    marker = "🟢" if val >= 0 else "🔴"
                    out.append(f"{marker} {line}")
            else:
                out.append(spacer + line)
    return "\n".join(out)


def pre_table(headers, rows, aligns=None, pnl_values=None):
    """Format, optionally P&L-annotate, HTML-escape, and wrap a table."""
    table = format_table(headers, rows, aligns=aligns)
    if pnl_values is not None:
        table = _annotate_pnl_emoji(table, pnl_values)
    return f"<pre>{html_escape(table)}</pre>"


def kv_table(rows, key_header="ITEM", value_header="VALUE"):
    """Two-column key/value table for Telegram HTML dashboards."""
    return pre_table([key_header, value_header], rows, aligns=["l", "l"])


def money(value, digits=2, signed=False):
    """Format a USDC-ish money value."""
    try:
        val = float(value)
    except Exception:
        return "$-"
    sign = "+" if signed else ""
    return f"${val:{sign}.{digits}f}"


def pct(value, digits=1, signed=True):
    """Format a percentage value."""
    try:
        val = float(value)
    except Exception:
        return "-"
    sign = "+" if signed else ""
    return f"{val:{sign}.{digits}f}%"


def pnl_emoji(value):
    try:
        return "🟢" if float(value) >= 0 else "🔴"
    except Exception:
        return "⚪"


def funding_flow(funding):
    """Return readable funding direction for a short position."""
    if funding is None:
        return "-"
    if funding > 0:
        return "GET"
    if funding < 0:
        return "PAY"
    return "FLAT"


def status_word(ok=None, warn=False):
    """Plain monospace-safe status token for tables."""
    if warn:
        return "WARN"
    if ok is True:
        return "OK"
    if ok is False:
        return "ERR"
    return "INFO"


def section(label):
    return f"\n<b>{html_escape(label)}</b>"


def format_duration(seconds):
    try:
        sec = int(float(seconds))
    except Exception:
        return str(seconds)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        hours = sec / 3600
        return f"{hours:.1f}h" if sec % 3600 else f"{sec // 3600}h"
    days = sec / 86400
    return f"{days:.1f}d" if sec % 86400 else f"{sec // 86400}d"

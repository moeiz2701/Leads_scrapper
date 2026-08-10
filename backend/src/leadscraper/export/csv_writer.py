"""§12.2 CSV export — the spreadsheet-armour layer.

Three requirements from §12.2, all of them things that bite once and then get
fixed forever:

* **UTF-8 with BOM.** Excel guesses the encoding of a BOM-less file from the
  system codepage, which on a Windows machine means cp1252 and means every Urdu
  and Arabic business name arrives as mojibake. The same defect shows up in this
  project's console output, where ``PYTHONIOENCODING=utf-8`` is the equivalent
  fix.
* **Phones as ``="+923001234567"``.** Excel reads a bare ``+923001234567`` as a
  number and renders it ``9.23001E+11``, destroying the one column the whole
  pipeline exists to produce.
* **``{city}_{category}_{YYYYMMDD}_{n}leads.csv``**, and the row set must be the
  filtered, sorted one the operator is looking at — which is why this is a
  server-side endpoint and not a client-side dump of the loaded page.

Output is streamed a row at a time so a 5,000-row export does not build a string
in memory, and so the first bytes reach the browser before the query finishes.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from typing import Any

from leadscraper.export.columns import COLUMNS, PHONE_VALUE_COLUMNS

# Spelled as an escape on purpose: a literal U+FEFF here is invisible in every
# editor and diff, and the one thing worse than a missing BOM is a BOM nobody
# can see to confirm.
BOM = "﻿"

# RFC 4180. Excel accepts LF too, but some Windows tooling in the operator's
# path (Notepad, older Power Query) still does not.
LINE_TERMINATOR = "\r\n"

_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9]+")


def _excel_text_formula(value: str) -> str:
    """Wrap a phone so Excel keeps it as text.

    The embedded quotes are escaped again by the CSV writer, so the field lands
    on disk doubled and wrapped. Excel unquotes that back to ``="+92…"``, treats
    the leading ``=`` as a formula, and evaluates a string literal to itself — so
    the cell holds the number as text with its ``+`` intact.
    ``test_the_wrapped_phone_survives_a_csv_round_trip`` pins the exact bytes.
    """
    return f'="{value}"'


def format_cell(column: str, value: Any) -> str:
    """One projected value → one CSV cell.

    ``None`` becomes an empty cell and never ``0``, ``"0"``, ``"None"`` or
    ``"-"``. §12.1 requires blanks for what was never published, and a
    placeholder string would be re-imported as data by whatever reads this file.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if column in PHONE_VALUE_COLUMNS:
        return _excel_text_formula(str(value))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def iter_csv(
    rows: Iterable[dict[str, Any]],
    columns: Iterable[str] = COLUMNS,
) -> Iterator[str]:
    """Stream a §12.1 row set as §12.2 CSV text, BOM first."""
    column_list = list(columns)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator=LINE_TERMINATOR)

    def _flush() -> str:
        text = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return text

    writer.writerow(column_list)
    yield BOM + _flush()

    for row in rows:
        writer.writerow([format_cell(name, row.get(name)) for name in column_list])
        yield _flush()


def write_csv(
    rows: Iterable[dict[str, Any]],
    columns: Iterable[str] = COLUMNS,
) -> str:
    """Non-streaming form. For tests and small exports."""
    return "".join(iter_csv(rows, columns))


def _slug(value: str | None, fallback: str) -> str:
    slug = _FILENAME_UNSAFE.sub("_", (value or "").strip()).strip("_")
    return slug or fallback


def export_filename(
    city: str | None,
    category: str | None,
    count: int,
    *,
    now: datetime | date | None = None,
) -> str:
    """§12.2's ``{city}_{category}_{YYYYMMDD}_{n}leads.csv``.

    ``city``/``category`` are ``None`` for a cross-run export spanning more than
    one of either (§10.1's read-side union), and become ``all`` rather than an
    empty segment — a filename with ``__`` in it looks like a bug to the person
    who receives the file.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d")
    return f"{_slug(city, 'all')}_{_slug(category, 'all')}_{stamp}_{count}leads.csv"

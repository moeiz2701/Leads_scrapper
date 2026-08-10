"""§12 output table and CSV export.

Split in two on purpose:

``columns``/``rows``  produce the §12.1 column set as **clean values** — this is
                      what the §13 Screen 3 table renders and what the API
                      serialises to JSON.
``csv_writer``        adds the §12.2 spreadsheet armour (BOM, ``="+92…"``) on
                      top of exactly those rows.

Keeping the Excel workarounds out of the projection is what lets the table and
the CSV be the same data by construction rather than by discipline — §12.2
requires the export to match what the operator sees on screen.
"""

from leadscraper.export.columns import (
    COLUMNS,
    COMPACT_COLUMNS,
    PHONE_SLOTS,
    phone_slot_columns,
)
from leadscraper.export.csv_writer import export_filename, write_csv
from leadscraper.export.rows import build_row

__all__ = [
    "COLUMNS",
    "COMPACT_COLUMNS",
    "PHONE_SLOTS",
    "build_row",
    "export_filename",
    "phone_slot_columns",
    "write_csv",
]

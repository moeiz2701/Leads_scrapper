"""§12.1 column set — 41 columns, 20 of which are the dynamic phone block.

§12.1 prints the columns as a numbered table; this is that table as data, so the
CSV header, the API's column metadata and the §12.3 compact view cannot drift
apart. ``test_export.py`` pins the order and the count against the doc.

The phone block is generated rather than written out: 4 slots × 5 columns, in
``contacts.rank`` order, which §3.3 wrote in Stage 5. Changing ``PHONE_SLOTS``
changes the column count, which is why §12.1's "41 columns" is asserted from
these lists rather than hard-coded.
"""

from __future__ import annotations

from typing import Final

# §12.1 caps the export at four ranked numbers. This is a *display* cap, never a
# retention one — §10.1's "never discard a contact" means the fifth number keeps
# its row and simply has no column to appear in.
PHONE_SLOTS: Final[int] = 4

# The five attributes each slot carries (§12.1 columns 11–15, repeated ×4).
PHONE_SLOT_ATTRIBUTES: Final[tuple[str, ...]] = (
    "",  # the E.164 value itself — `phone_1`, no suffix
    "_type",
    "_whatsapp",
    "_belongs_to",
    "_source",
)

_IDENTITY: Final[tuple[str, ...]] = (
    "business_name",
    "category",
    "subcategory",
    "city",
    "area",
    "address",
)

_PERSON: Final[tuple[str, ...]] = (
    "contact_person",
    "contact_role",
    "attribution",
)

_OTHER_CONTACT: Final[tuple[str, ...]] = (
    "email",
    "website",
    "facebook_url",
    "instagram_url",
    "maps_url",
)

_QUALITY: Final[tuple[str, ...]] = (
    "rating",
    "review_count",
    "lead_score",
)

_PROVENANCE: Final[tuple[str, ...]] = (
    "sources",
    "evidence_urls",
    "scraped_at",
)


def phone_slot_columns(slot: int) -> tuple[str, ...]:
    """The five column names for one 1-indexed phone slot."""
    return tuple(f"phone_{slot}{suffix}" for suffix in PHONE_SLOT_ATTRIBUTES)


def _phone_block() -> tuple[str, ...]:
    return tuple(
        name for slot in range(1, PHONE_SLOTS + 1) for name in phone_slot_columns(slot)
    )


COLUMNS: Final[tuple[str, ...]] = (
    *_IDENTITY,
    *_PERSON,
    "phone_count",
    *_phone_block(),
    *_OTHER_CONTACT,
    *_QUALITY,
    *_PROVENANCE,
)

# §12.3 — "nobody wants 41 columns on screen". The default table view; everything
# else lives behind the column-visibility toggle, and the CSV always exports the
# full set.
COMPACT_COLUMNS: Final[tuple[str, ...]] = (
    "business_name",
    "area",
    "contact_person",
    "contact_role",
    "phone_1",
    "phone_1_whatsapp",
    "phone_2",
    "phone_count",
    "website",
    "rating",
    "lead_score",
    "sources",
)

# Columns whose values are phone numbers. §12.2 writes these as `="+92…"` so
# Excel does not reformat them into scientific notation; nothing else needs it,
# and applying it to `phone_1_type` would put `="mobile"` in the cell.
PHONE_VALUE_COLUMNS: Final[frozenset[str]] = frozenset(
    f"phone_{slot}" for slot in range(1, PHONE_SLOTS + 1)
)

# Numeric columns, for the API's column metadata and the table's right-alignment.
# `review_count` is here and is still blank when missing — §10.2's load-bearing
# rule is that a field the source never published is not a zero.
NUMERIC_COLUMNS: Final[frozenset[str]] = frozenset(
    {"phone_count", "rating", "review_count", "lead_score"}
)

"""§12.1 column set and §12.2 CSV. Pure — no database.

Every test here is pinned to a clause in §12 that costs the operator something
real when it breaks: a mangled phone column, an Urdu name that arrives as
mojibake, or a blank that got exported as a zero and then acted on.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime

from leadscraper.enums import (
    Attribution,
    BelongsTo,
    ContactKind,
    LineType,
    PersonRole,
    Source,
    WhatsAppLabel,
)
from leadscraper.export import COLUMNS, COMPACT_COLUMNS, PHONE_SLOTS, build_row
from leadscraper.export.csv_writer import BOM, export_filename, format_cell, write_csv

NOW = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)


@dataclass
class FakeContact:
    value_e164: str | None = "+923001234567"
    value_raw: str = "0300 1234567"
    kind: str = ContactKind.PHONE
    line_type: str | None = LineType.MOBILE
    wa_label: str | None = WhatsAppLabel.LIKELY
    belongs_to: str | None = BelongsTo.BUSINESS
    person_name: str | None = None
    person_role: str | None = None
    attribution: str | None = None
    source: str = Source.GOOGLE_MAPS
    source_url: str = "https://maps.google.com/?cid=1"
    wa_evidence_url: str | None = None
    rank: int | None = 1
    scraped_at: datetime | None = NOW


@dataclass
class FakeBusiness:
    name: str = "Paragon Salon"
    category: str | None = "salon"
    subcategory: str | None = None
    city: str | None = "Islamabad"
    area: str | None = "F-7"
    address: str | None = "Jinnah Super Market"
    place_id: str | None = "ChIJabc123"
    website: str | None = "https://paragon.pk"
    facebook_url: str | None = None
    instagram_url: str | None = None
    rating: float | None = 4.6
    review_count: int | None = 31
    lead_score: int | None = 72
    created_at: datetime | None = NOW


# --------------------------------------------------------------------------- #
# §12.1 — the column set
# --------------------------------------------------------------------------- #


def test_column_set_is_the_41_columns_section_12_1_specifies():
    assert len(COLUMNS) == 41
    assert len(set(COLUMNS)) == 41, "a duplicated column name would silently overwrite"


def test_phone_block_is_twenty_of_the_forty_one():
    """§12.1: "4 slots × 5 columns = 20 of the 41"."""
    phone_columns = [c for c in COLUMNS if c.startswith("phone_") and c != "phone_count"]
    assert len(phone_columns) == PHONE_SLOTS * 5 == 20


def test_column_order_matches_the_doc_at_its_named_boundaries():
    assert COLUMNS[0] == "business_name"
    assert COLUMNS[6] == "contact_person"  # column 7
    assert COLUMNS[9] == "phone_count"  # column 10
    assert COLUMNS[10] == "phone_1"  # column 11
    assert COLUMNS[30] == "email"  # column 31
    assert COLUMNS[-1] == "scraped_at"  # column 41


def test_compact_view_is_twelve_columns_and_a_subset(bare=None):
    """§12.3 — "nobody wants 41 columns on screen"."""
    assert len(COMPACT_COLUMNS) == 12
    assert set(COMPACT_COLUMNS) <= set(COLUMNS)


# --------------------------------------------------------------------------- #
# §12.1 — missing stays missing
# --------------------------------------------------------------------------- #


def test_absent_review_count_exports_blank_not_zero():
    """§10.2's load-bearing rule, carried into the export.

    ``review_count`` is present on 80% of the Islamabad run and 0% of the Lahore
    one. Exporting the Lahore gap as ``0`` would tell the operator every salon in
    Lahore has no reviews, which is false and which they would act on.
    """
    row = build_row(FakeBusiness(review_count=None, rating=None), [FakeContact()])
    assert row["review_count"] is None
    assert row["rating"] is None
    assert format_cell("review_count", row["review_count"]) == ""


def test_unattributed_business_exports_a_blank_contact_person():
    """§12.1 column 7: "Blank when unattributed — never guessed"."""
    row = build_row(FakeBusiness(), [FakeContact(person_name=None)])
    assert row["contact_person"] is None
    assert row["contact_role"] is None
    assert row["attribution"] is None


def test_person_columns_all_come_from_one_contact():
    """§8: never fabricate the join.

    A name from one number and a role from another is an attribution we never
    made. The three columns describe one person, so they are taken together.
    """
    unnamed = FakeContact(value_e164="+923001111111", rank=1, person_role=PersonRole.OWNER)
    named = FakeContact(
        value_e164="+923002222222",
        rank=2,
        person_name="Hina Khan",
        person_role=PersonRole.LIKELY_OWNER,
        attribution=Attribution.INFERRED,
    )
    row = build_row(FakeBusiness(), [unnamed, named])
    assert row["contact_person"] == "Hina Khan"
    # Not `owner` from the higher-ranked row that has no name.
    assert row["contact_role"] == PersonRole.LIKELY_OWNER
    assert row["attribution"] == Attribution.INFERRED


# --------------------------------------------------------------------------- #
# §12.1 — the phone block
# --------------------------------------------------------------------------- #


def test_phone_slots_follow_contacts_rank():
    contacts = [
        FakeContact(value_e164="+923003333333", rank=3),
        FakeContact(value_e164="+923001111111", rank=1),
        FakeContact(value_e164="+923002222222", rank=2),
    ]
    row = build_row(FakeBusiness(), contacts)
    assert row["phone_1"] == "+923001111111"
    assert row["phone_2"] == "+923002222222"
    assert row["phone_3"] == "+923003333333"
    assert row["phone_4"] is None


def test_unranked_contacts_never_take_a_slot():
    """§3.3 — ``rank = None`` is ``whatsapp_only``'s filter and the §10.1
    duplicate-provenance case. Neither belongs in a column."""
    contacts = [
        FakeContact(value_e164="+923001111111", rank=1),
        FakeContact(value_e164="+923009999999", rank=None),
    ]
    row = build_row(FakeBusiness(), contacts)
    assert row["phone_1"] == "+923001111111"
    assert row["phone_2"] is None


def test_phone_count_reports_numbers_beyond_the_four_slots():
    """The cap is on columns, not on the count — §10.1 never discards a contact.

    An operator who sees four numbers needs to know there are seven.
    """
    contacts = [FakeContact(value_e164=f"+92300000000{i}", rank=i) for i in range(1, 8)]
    row = build_row(FakeBusiness(), contacts)
    assert row["phone_count"] == 7
    assert row["phone_4"] == "+923000000004"


def test_phone_count_counts_numbers_not_provenance_rows():
    """§10.1: one number can sit on two rows after a merge, each a real record."""
    contacts = [
        FakeContact(value_e164="+923001111111", rank=1, source=Source.GOOGLE_MAPS),
        FakeContact(value_e164="+923001111111", rank=None, source=Source.BUSINESS_WEBSITE),
    ]
    assert build_row(FakeBusiness(), contacts)["phone_count"] == 1


def test_export_shows_the_whatsapp_label_never_the_raw_score():
    """§9.3 — the operator sees confirmed/likely/no; ``wa_evidence`` is internal."""
    row = build_row(FakeBusiness(), [FakeContact(wa_label=WhatsAppLabel.CONFIRMED)])
    assert row["phone_1_whatsapp"] == WhatsAppLabel.CONFIRMED
    assert not any("evidence" in c for c in COLUMNS if c.startswith("phone_"))


# --------------------------------------------------------------------------- #
# §1 — provenance survives
# --------------------------------------------------------------------------- #


def test_evidence_urls_carry_both_the_value_and_the_proof():
    """§5.2: the page that published a number and the page that proved it are
    routinely different, which is why ``wa_evidence_url`` exists at all."""
    contact = FakeContact(
        source_url="https://maps.google.com/?cid=1",
        wa_evidence_url="https://paragon.pk/contact",
    )
    row = build_row(FakeBusiness(), [contact])
    assert row["evidence_urls"] == "https://maps.google.com/?cid=1|https://paragon.pk/contact"


def test_provenance_spans_contacts_that_have_no_column():
    """The fifth number's source is as real as the first's, and §15's deletion
    path depends on it staying accurate."""
    contacts = [FakeContact(value_e164=f"+92300000000{i}", rank=i) for i in range(1, 6)]
    contacts[4].source = Source.BUSINESS_WEBSITE
    contacts[4].source_url = "https://paragon.pk/contact"
    row = build_row(FakeBusiness(), contacts)
    assert Source.BUSINESS_WEBSITE in row["sources"]
    assert "https://paragon.pk/contact" in row["evidence_urls"]


def test_maps_url_is_derived_from_place_id_and_blank_without_one():
    assert "ChIJabc123" in build_row(FakeBusiness(), [FakeContact()])["maps_url"]
    assert build_row(FakeBusiness(place_id=None), [FakeContact()])["maps_url"] is None


# --------------------------------------------------------------------------- #
# §12.2 — the CSV itself
# --------------------------------------------------------------------------- #


def test_csv_starts_with_a_bom_so_excel_opens_urdu_names():
    """§12.2. Without the BOM, Excel guesses the system codepage — cp1252 on this
    machine — and every Urdu and Arabic name arrives as mojibake."""
    csv_text = write_csv([build_row(FakeBusiness(name="بیوٹی سیلون"), [FakeContact()])])
    assert csv_text.startswith(BOM)
    assert "بیوٹی سیلون" in csv_text
    # And it must survive the round trip an operator's spreadsheet does.
    decoded = csv_text.encode("utf-8").decode("utf-8-sig")
    assert "بیوٹی سیلون" in decoded


def test_phone_cells_are_wrapped_so_excel_does_not_mangle_them():
    """§12.2 — a bare ``+923001234567`` renders as ``9.23001E+11``."""
    assert format_cell("phone_1", "+923001234567") == '="+923001234567"'
    # The attribute columns beside it are ordinary text.
    assert format_cell("phone_1_type", LineType.MOBILE) == "mobile"
    assert format_cell("phone_1_whatsapp", WhatsAppLabel.CONFIRMED) == "confirmed"


def test_the_wrapped_phone_survives_a_csv_round_trip():
    """The wrapper contains quotes, so the CSV writer escapes them again. What
    lands in the cell must still be the formula Excel evaluates back to text."""
    csv_text = write_csv([build_row(FakeBusiness(), [FakeContact()])])
    rows = list(csv.reader(io.StringIO(csv_text.removeprefix(BOM))))
    header, data = rows[0], rows[1]
    assert data[header.index("phone_1")] == '="+923001234567"'


def test_blank_cells_are_empty_not_placeholders():
    """A ``-`` or a ``None`` would be re-imported as data by whatever reads this."""
    row = build_row(FakeBusiness(review_count=None, subcategory=None), [FakeContact()])
    csv_text = write_csv([row])
    rows = list(csv.reader(io.StringIO(csv_text.removeprefix(BOM))))
    header, data = rows[0], rows[1]
    assert data[header.index("review_count")] == ""
    assert data[header.index("subcategory")] == ""


def test_header_is_the_full_column_set_even_when_a_row_is_sparse():
    csv_text = write_csv([build_row(FakeBusiness(), [])])
    header = next(csv.reader(io.StringIO(csv_text.removeprefix(BOM))))
    assert header == list(COLUMNS)


def test_export_filename_follows_section_12_2():
    name = export_filename("Islamabad", "salon", 45, now=datetime(2026, 8, 10))
    assert name == "Islamabad_salon_20260810_45leads.csv"


def test_export_filename_names_a_cross_run_export_rather_than_leaving_a_gap():
    """§10.1's read-side union can span cities; ``__`` reads as a bug."""
    name = export_filename(None, None, 72, now=datetime(2026, 8, 10))
    assert name == "all_all_20260810_72leads.csv"
    assert "__" not in name


def test_export_filename_is_filesystem_safe():
    name = export_filename("Dera Ghazi Khan", "car_services", 3, now=datetime(2026, 8, 10))
    assert name == "Dera_Ghazi_Khan_car_services_20260810_3leads.csv"

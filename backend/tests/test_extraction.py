"""Extraction — the top-N pull and the ledger behind it.

The tests that carry weight here are the ones about what a pull *refuses* to do:
copy a §15-suppressed number, offer a business it already handed out, or take a
number §9.3 labelled `no`. Each of those is a number the operator would ring, and
the whole feature exists so that the list they ring is short, fresh and defensible.

``test_the_pull_is_the_table`` is the §12.2 invariant one output further along:
the screen, the CSV and the clipboard are one query, so "extract whatever filter
is applied" is true by construction rather than by three call sites agreeing.
"""

from __future__ import annotations

import itertools
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from leadscraper.core import batches
from leadscraper.db.models import Business, Contact, DoNotContact, Extraction, Run
from leadscraper.enums import (
    ContactKind,
    LineType,
    NumberPreference,
    RunStatus,
    Source,
    WhatsAppLabel,
)
from leadscraper.services.extraction import (
    BATCH_SIZES,
    EXTRACT_ALL,
    UnsupportedBatchSize,
    batch_counts,
    clear_extraction,
    clear_extractions,
    extract,
    extractable_numbers,
    list_extractions,
)
from leadscraper.services.results import ResultQuery, fetch_results
from tests.conftest import requires_db

_SEQUENCE = itertools.count(1)

SMALLEST = BATCH_SIZES[0]  # 30


def _number() -> str:
    return f"+9230066{next(_SEQUENCE):05d}"


def _run(session: Session, city="Lahore", category="salon") -> Run:
    run = Run(
        city=city,
        category=category,
        number_pref=NumberPreference.OWNER_FIRST,
        sources_enabled={"google_maps": True},
        status=RunStatus.DONE,
    )
    session.add(run)
    session.flush()
    return run


def _business(session: Session, run: Run, **overrides) -> Business:
    base = dict(
        name="Paragon Salon",
        name_norm="paragon salon",
        city=run.city,
        place_id=f"place-{uuid.uuid4()}",
        lead_score=70,
    )
    business = Business(run_id=run.id, **{**base, **overrides})
    session.add(business)
    session.flush()
    return business


def _phone(session: Session, business: Business, e164: str | None = None, **overrides) -> Contact:
    base = dict(
        kind=ContactKind.PHONE,
        value_raw="0300 6600001",
        value_e164=e164 or _number(),
        line_type=LineType.MOBILE,
        wa_evidence=0.60,
        wa_label=WhatsAppLabel.LIKELY,
        source=Source.GOOGLE_MAPS,
        source_url="https://maps.google.com/?cid=1",
        rank=1,
    )
    contact = Contact(business_id=business.id, **{**base, **overrides})
    session.add(contact)
    session.flush()
    return contact


def _lead(session: Session, run: Run, name: str, score: int, **overrides) -> Business:
    """A business with one `likely` mobile — the shape most rows in the live data have."""
    business = _business(
        session, run, name=name, name_norm=name.lower(), lead_score=score, **overrides
    )
    _phone(session, business)
    return business


# --------------------------------------------------------------------------- #
# Which numbers qualify (pure)
# --------------------------------------------------------------------------- #


class _FakeContact:
    def __init__(self, **kwargs):
        self.kind = kwargs.get("kind", ContactKind.PHONE)
        self.value_e164 = kwargs.get("value_e164")
        self.value_raw = kwargs.get("value_raw", "")
        self.wa_label = kwargs.get("wa_label", WhatsAppLabel.LIKELY)
        self.rank = kwargs.get("rank", 1)


def test_only_confirmed_and_likely_numbers_are_taken():
    """§9.3's `no` is the record arguing against the number, not an unknown.

    `confirmed` and `likely` are both "the public record says this takes
    WhatsApp, at different strengths". `no` is the one label that says the
    opposite, so it is the one the pull must never copy.
    """
    contacts = [
        _FakeContact(value_e164="+923001111111", wa_label=WhatsAppLabel.CONFIRMED, rank=1),
        _FakeContact(value_e164="+923002222222", wa_label=WhatsAppLabel.LIKELY, rank=2),
        _FakeContact(value_e164="+924235555555", wa_label=WhatsAppLabel.NO, rank=3),
        _FakeContact(value_e164="+923004444444", wa_label=None, rank=4),
    ]
    assert extractable_numbers(contacts) == ["+923001111111", "+923002222222"]


def test_every_qualifying_number_is_taken_not_just_the_four_export_slots():
    """§12.1 caps at 4 phone columns; §10.1 forbids that becoming a data cap.

    The 4-slot limit is a property of a spreadsheet column set. The operator
    asked for every number each business has, and a business with six is a
    business with six.
    """
    contacts = [
        _FakeContact(value_e164=f"+92300000000{i}", rank=i) for i in range(1, 7)
    ]
    assert len(extractable_numbers(contacts)) == 6


def test_an_unranked_duplicate_provenance_row_is_not_copied_twice():
    """``rank is None`` means excluded by §3.3 or a §10.1 merge's second row.

    Either way the number is not one the operator can see in the table, and in
    the merge case it is a duplicate of one they can — copying it would put the
    same number on the clipboard twice.
    """
    contacts = [
        _FakeContact(value_e164="+923001111111", rank=1),
        _FakeContact(value_e164="+923001111111", rank=None),
    ]
    assert extractable_numbers(contacts) == ["+923001111111"]


def test_an_email_is_never_mistaken_for_a_number():
    contacts = [
        _FakeContact(kind=ContactKind.EMAIL, value_raw="hi@salon.pk", value_e164=None),
        _FakeContact(value_e164="+923001111111"),
    ]
    assert extractable_numbers(contacts) == ["+923001111111"]


# --------------------------------------------------------------------------- #
# The pull
# --------------------------------------------------------------------------- #


@requires_db
def test_the_pull_takes_the_top_of_the_table_in_score_order(db_session: Session):
    run = _run(db_session)
    for name, score in [("Low", 40), ("High", 90), ("Mid", 70)]:
        _lead(db_session, run, name, score)

    result = extract(db_session, ResultQuery(run_ids=(run.id,)), SMALLEST)

    assert [b.name for b in result.businesses] == ["High", "Mid", "Low"]
    assert result.marked == 3


@requires_db
def test_the_second_pull_skips_what_the_first_pull_took(db_session: Session):
    """The ledger is the reason this feature exists.

    Two pulls of 30 have to be 60 distinct businesses. If the second pull
    re-offered the first pull's rows the mark would be a decoration, and the
    operator would message the same salons twice.
    """
    run = _run(db_session)
    for i in range(4):
        _lead(db_session, run, f"Salon {i}", 90 - i)

    query = ResultQuery(run_ids=(run.id,))
    first = extract(db_session, query, SMALLEST)
    second = extract(db_session, query, SMALLEST)

    assert first.marked == 4
    assert second.marked == 0
    assert second.skipped_already_extracted == 4
    assert second.numbers == []
    # And it says so rather than returning an empty clipboard silently.
    assert any("no more un-extracted rows" in w for w in second.warnings)


@requires_db
def test_a_pull_shorter_than_the_batch_reports_what_is_left(db_session: Session):
    run = _run(db_session)
    for i in range(5):
        _lead(db_session, run, f"Salon {i}", 90 - i)

    result = extract(db_session, ResultQuery(run_ids=(run.id,)), SMALLEST)
    assert result.requested == SMALLEST
    assert result.marked == 5
    assert result.remaining == 0


@requires_db
def test_remaining_counts_the_un_extracted_rows_the_pull_left_behind(db_session: Session):
    """Three pulls of 30 over 100 rows should not need the operator to guess."""
    run = _run(db_session)
    for i in range(35):
        _lead(db_session, run, f"Salon {i:02d}", 90)

    result = extract(db_session, ResultQuery(run_ids=(run.id,)), SMALLEST)
    assert result.marked == 30
    assert result.remaining == 5


@requires_db
def test_a_business_with_no_qualifying_number_still_counts_and_is_still_marked(
    db_session: Session,
):
    """"Top 30" counts businesses worked, not numbers found.

    A row whose only number is a §9.3 `no` was looked at and found wanting.
    Leaving it unmarked would offer the same dead row on every pull for ever, so
    it is marked — and the count is reported, because a clipboard shorter than
    the batch must be explained rather than inferred.
    """
    run = _run(db_session)
    good = _lead(db_session, run, "Good", 90)
    dead = _business(db_session, run, name="Dead", lead_score=80)
    _phone(db_session, dead, wa_label=WhatsAppLabel.NO)

    result = extract(db_session, ResultQuery(run_ids=(run.id,)), SMALLEST)

    assert result.marked == 2
    assert result.without_numbers == 1
    assert len(result.numbers) == 1
    assert {b.business_id for b in result.businesses} == {good.id, dead.id}
    assert any("contributed nothing to the clipboard" in w for w in result.warnings)


@requires_db
def test_the_pull_is_the_table(db_session: Session):
    """§12.2's "respect the active filters", one output further along.

    The clipboard is a third rendering of the same query as the screen and the
    CSV. An extraction that read its own query would be the defect §12.2 defines
    for the CSV, wearing a different button.
    """
    run = _run(db_session)
    _lead(db_session, run, "Sited", 90, website="https://sited.pk")
    _lead(db_session, run, "Siteless", 95, website=None)

    query = ResultQuery(run_ids=(run.id,), has_website=True)
    table = fetch_results(db_session, query)
    result = extract(db_session, query, SMALLEST)

    assert [r["business_name"] for r in table.rows] == ["Sited"]
    assert [b.name for b in result.businesses] == ["Sited"]
    # The filtered-out business is untouched — not marked, still extractable.
    assert result.marked == 1


@requires_db
def test_a_suppressed_number_never_reaches_the_clipboard(db_session: Session):
    """§15, checked here because the clipboard is the sharpest read there is.

    A suppressed number rendered in a table is a number that might be rung. A
    suppressed number pasted into a bulk-messaging tool is a number that
    definitely is.
    """
    run = _run(db_session)
    business = _lead(db_session, run, "Paragon", 90)
    keep = _phone(db_session, business, wa_label=WhatsAppLabel.CONFIRMED, rank=2)
    blocked = business.contacts[0].value_e164
    db_session.add(DoNotContact(value_e164=blocked, reason="removal request"))
    db_session.flush()

    result = extract(db_session, ResultQuery(run_ids=(run.id,)), SMALLEST)

    assert result.numbers == [keep.value_e164]
    assert blocked not in result.numbers


@requires_db
def test_the_same_number_on_two_businesses_is_copied_once(db_session: Session):
    """A shared landline is one number to message, however many branches list it."""
    run = _run(db_session)
    shared = _number()
    for name in ("Branch A", "Branch B"):
        business = _business(db_session, run, name=name, lead_score=80)
        _phone(db_session, business, shared)

    result = extract(db_session, ResultQuery(run_ids=(run.id,)), SMALLEST)

    assert result.numbers == [shared]
    # Both businesses are still marked: two rows were worked, one number came out.
    assert result.marked == 2


@requires_db
def test_the_clipboard_is_one_number_per_line(db_session: Session):
    """The format is pinned server-side so it has exactly one definition.

    A dialler, a spreadsheet column and a bulk-messaging paste box all read
    newline-separated values; a comma-joined line reads as a single field in two
    of the three.
    """
    run = _run(db_session)
    _lead(db_session, run, "A", 90)
    _lead(db_session, run, "B", 80)

    result = extract(db_session, ResultQuery(run_ids=(run.id,)), SMALLEST)

    assert result.clipboard == "\n".join(result.numbers)
    assert len(result.clipboard.splitlines()) == 2


@requires_db
def test_an_unsupported_batch_size_is_refused_rather_than_clamped(db_session: Session):
    """This endpoint writes rows; a mis-typed size would empty the queue in one go."""
    run = _run(db_session)
    _lead(db_session, run, "A", 90)

    with pytest.raises(UnsupportedBatchSize):
        extract(db_session, ResultQuery(run_ids=(run.id,)), 31)

    assert db_session.execute(select(Extraction)).scalars().all() == []


# --------------------------------------------------------------------------- #
# Extract all
# --------------------------------------------------------------------------- #


@requires_db
def test_extract_all_drains_the_filtered_view(db_session: Session):
    """Working a batch means finishing it.

    Two rounds of top-30 and a third that returns 11 is one pull said three
    times, and each round is a chance to lose count of which rows are left.
    """
    run = _run(db_session)
    for i in range(35):
        _lead(db_session, run, f"Salon {i:02d}", 90)

    result = extract(db_session, ResultQuery(run_ids=(run.id,)), EXTRACT_ALL)

    assert result.marked == 35
    assert result.remaining == 0
    # `requested` is None rather than 35: "everything that was left" is a
    # different fact from "top 35", and recording the realised count as the
    # request would turn the first into the second.
    assert result.requested is None
    assert result.warnings == []


@requires_db
def test_extract_all_is_still_only_what_is_filtered(db_session: Session):
    """"All" is a size, not a scope. It never reaches past the current view.

    This is what makes it safe to point at one batch: the button drains
    `delivery-nosite` and leaves the other six untouched.
    """
    run = _run(db_session)
    _lead(db_session, run, "Sited", 90, website="https://sited.pk")
    _lead(db_session, run, "Siteless", 95, website=None)

    result = extract(
        db_session, ResultQuery(run_ids=(run.id,), has_website=True), EXTRACT_ALL
    )

    assert [b.name for b in result.businesses] == ["Sited"]
    assert len(db_session.execute(select(Extraction)).scalars().all()) == 1


@requires_db
def test_extract_all_on_an_exhausted_view_says_so(db_session: Session):
    """A pull that marks nothing must explain itself rather than returning an
    empty clipboard and letting the operator wonder."""
    run = _run(db_session)
    _lead(db_session, run, "A", 90)
    query = ResultQuery(run_ids=(run.id,))

    extract(db_session, query, EXTRACT_ALL)
    second = extract(db_session, query, EXTRACT_ALL)

    assert second.marked == 0
    assert any("no more un-extracted rows" in w for w in second.warnings)


@requires_db
def test_an_all_pull_records_no_batch_size(db_session: Session):
    """NULL ``batch_size`` is "everything that was left", not a missing value."""
    run = _run(db_session)
    _lead(db_session, run, "A", 90)

    extract(db_session, ResultQuery(run_ids=(run.id,)), EXTRACT_ALL)

    entry = db_session.execute(select(Extraction)).scalars().one()
    assert entry.batch_size is None


# --------------------------------------------------------------------------- #
# The outreach batch on the ledger (_BATCH_SPEC.md)
# --------------------------------------------------------------------------- #


def _food(session: Session, run: Run, name: str, **overrides) -> Business:
    base = dict(category="food", subcategory="Restaurant", review_count=800, rating=4.5)
    business = _business(
        session, run, name=name, name_norm=name.lower(), **{**base, **overrides}
    )
    _phone(session, business)
    return business


@requires_db
def test_a_pull_can_be_scoped_to_one_batch(db_session: Session):
    """"Give me the next 30 Commission Escape leads" — the operator's actual loop.

    The batch is a filter like any other, so it reaches the pull through the same
    query the table used. Nothing in the extraction path knows what a batch is
    beyond recording which one it was.
    """
    run = _run(db_session, category="food")
    _food(db_session, run, "No Site")
    _food(db_session, run, "Sited", website="https://sited.pk")

    result = extract(
        db_session,
        ResultQuery(run_ids=(run.id,), batches=(batches.DELIVERY_NOSITE,)),
        EXTRACT_ALL,
    )

    assert [b.name for b in result.businesses] == ["No Site"]
    assert [b.batch for b in result.businesses] == [batches.DELIVERY_NOSITE]


@requires_db
def test_the_ledger_records_the_batch_as_it_was_when_the_message_went_out(
    db_session: Session,
):
    """The batch is history, like the numbers beside it.

    This business had no website when it was messaged the `delivery-nosite`
    pitch — an argument about Foodpanda commission aimed at a business with no
    web presence. A later run finds its site and the cascade moves it to
    `delivery-site`, whose pitch is an addition to a site it now has. Rewriting
    the ledger would erase which of the two was actually sent, and the follow-up
    would contradict the first message.
    """
    run = _run(db_session, category="food")
    business = _food(db_session, run, "Karahi House")

    extract(db_session, ResultQuery(run_ids=(run.id,)), SMALLEST)

    business.website = "https://karahi.pk"
    db_session.flush()

    entry = list_extractions(db_session, (run.id,))[0]
    assert entry["batch"] == batches.DELIVERY_NOSITE
    # ...and the move is visible rather than silent.
    assert entry["current_batch"] == batches.DELIVERY_SITE


@requires_db
def test_the_ledger_can_be_filtered_to_one_batch(db_session: Session):
    """"What did I already send the Café First Presence message to?"

    The question that decides what today's pull can say, and it has to be
    answerable per run and per batch.
    """
    run = _run(db_session, category="food")
    _food(db_session, run, "Cafe One", subcategory="Cafe")
    _food(db_session, run, "Delivery One")

    extract(db_session, ResultQuery(run_ids=(run.id,)), EXTRACT_ALL)

    cafes = list_extractions(
        db_session, (run.id,), batch_tokens=(batches.CAFE_NOSITE,)
    )
    assert [e["business_name"] for e in cafes] == ["Cafe One"]
    # The same tokens the results filter takes, spelled either way.
    assert len(list_extractions(db_session, (run.id,), batch_tokens=("B03",))) == 1
    assert batch_counts(db_session, (run.id,))[batches.CAFE_NOSITE] == 1


@requires_db
def test_a_row_pulled_before_the_column_existed_falls_back_to_its_current_batch(
    db_session: Session,
):
    """``None`` and ``unbatched`` are different answers and are stored apart.

    A NULL ``batch`` means "we did not record one" — the row predates the column.
    Backfilling it would assert a fact about a past pull from present data, so
    the read falls back to the current batch and the UI marks it as derived. The
    alternative is a screen that is empty for every pull made before this
    feature landed.
    """
    run = _run(db_session, category="food")
    _food(db_session, run, "Old Pull")
    extract(db_session, ResultQuery(run_ids=(run.id,)), SMALLEST)

    entry = db_session.execute(select(Extraction)).scalars().one()
    entry.batch = None  # as the ledger looked before the migration
    db_session.flush()

    row = list_extractions(db_session, (run.id,))[0]
    assert row["batch"] is None
    assert row["current_batch"] == batches.DELIVERY_NOSITE
    # ...and it is still findable under the batch it is in today.
    assert (
        len(list_extractions(db_session, (run.id,), batch_tokens=(batches.DELIVERY_NOSITE,)))
        == 1
    )


@requires_db
def test_a_non_food_pull_is_recorded_as_unbatched(db_session: Session):
    """Not NULL: "the cascade has no definition covering this business" is a
    fact we do know, and it is not the same as "we did not record one"."""
    run = _run(db_session, city="Lahore", category="salon")
    _lead(db_session, run, "Glow Studio", 90, category="salon")

    extract(db_session, ResultQuery(run_ids=(run.id,)), SMALLEST)

    entry = db_session.execute(select(Extraction)).scalars().one()
    assert entry.batch == batches.UNBATCHED


@requires_db
def test_clearing_is_scoped_to_the_batch_the_screen_was_showing(db_session: Session):
    """A clear must retract exactly what was listed.

    Ignoring the batch filter would delete a run's whole ledger from a screen
    displaying one batch of it — and the operator would find out by re-extracting
    businesses they had already messaged.
    """
    run = _run(db_session, category="food")
    _food(db_session, run, "Cafe One", subcategory="Cafe")
    _food(db_session, run, "Delivery One")
    _food(db_session, run, "Delivery Two", website="https://two.pk")

    extract(db_session, ResultQuery(run_ids=(run.id,)), EXTRACT_ALL)

    cleared = clear_extractions(
        db_session, (run.id,), batch_tokens=(batches.CAFE_NOSITE,)
    )

    assert cleared == 1
    assert sorted(e["business_name"] for e in list_extractions(db_session)) == [
        "Delivery One",
        "Delivery Two",
    ]


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #


@requires_db
def test_the_ledger_records_the_numbers_as_they_were_sent(db_session: Session):
    """A record of what went out, not a live view of what the business has now.

    A later run raising a contact's §9.3 evidence would otherwise rewrite the
    history of a message that has already been sent.
    """
    run = _run(db_session)
    business = _lead(db_session, run, "Paragon", 90)
    sent = business.contacts[0].value_e164

    extract(db_session, ResultQuery(run_ids=(run.id,)), SMALLEST)
    _phone(db_session, business, wa_label=WhatsAppLabel.CONFIRMED, rank=2)
    db_session.flush()

    entries = list_extractions(db_session, (run.id,))
    assert len(entries) == 1
    assert entries[0]["numbers"] == [sent]
    assert entries[0]["business_name"] == "Paragon"


@requires_db
def test_clearing_one_entry_makes_that_business_extractable_again(db_session: Session):
    run = _run(db_session)
    _lead(db_session, run, "A", 90)
    _lead(db_session, run, "B", 80)

    query = ResultQuery(run_ids=(run.id,))
    extract(db_session, query, SMALLEST)
    entry = next(e for e in list_extractions(db_session, (run.id,)) if e["business_name"] == "A")

    assert clear_extraction(db_session, entry["id"]) is True

    again = extract(db_session, query, SMALLEST)
    assert [b.name for b in again.businesses] == ["A"]


@requires_db
def test_clearing_the_ledger_deletes_no_business_and_no_suppression(db_session: Session):
    """"I have not sent to these after all" is the only claim being retracted.

    §15's list is a different statement — "never contact this" — and clearing an
    extraction must not touch it in either direction.
    """
    run = _run(db_session)
    _lead(db_session, run, "A", 90)
    _lead(db_session, run, "B", 80)
    db_session.add(DoNotContact(value_e164="+923009999999", reason="unrelated"))
    db_session.flush()

    extract(db_session, ResultQuery(run_ids=(run.id,)), SMALLEST)
    assert clear_extractions(db_session) == 2

    assert list_extractions(db_session) == []
    assert len(db_session.execute(select(Business)).scalars().all()) == 2
    assert len(db_session.execute(select(DoNotContact)).scalars().all()) == 1


@requires_db
def test_clearing_one_run_leaves_another_runs_ledger_alone(db_session: Session):
    first = _run(db_session, city="Lahore")
    second = _run(db_session, city="Islamabad")
    _lead(db_session, first, "Lahore A", 90)
    _lead(db_session, second, "Islamabad A", 90)

    extract(db_session, ResultQuery(run_ids=(first.id,)), SMALLEST)
    extract(db_session, ResultQuery(run_ids=(second.id,)), SMALLEST)

    assert clear_extractions(db_session, (first.id,)) == 1
    assert [e["business_name"] for e in list_extractions(db_session)] == ["Islamabad A"]


@requires_db
def test_the_table_marks_extracted_rows_without_hiding_them(db_session: Session):
    """The mark decorates; it never filters.

    Hiding extracted rows would shrink the table and — because §12.2 is the same
    query — the CSV with it, so a business would vanish from an export because
    somebody once copied its number.
    """
    run = _run(db_session)
    _lead(db_session, run, "A", 90)
    _lead(db_session, run, "B", 80)

    query = ResultQuery(run_ids=(run.id,))
    assert [r["_extracted"] for r in fetch_results(db_session, query).rows] == [False, False]

    extract(db_session, query, SMALLEST)

    page = fetch_results(db_session, query)
    assert page.total == 2
    assert [r["_extracted"] for r in page.rows] == [True, True]


@requires_db
def test_deleting_a_business_takes_its_ledger_row_with_it(db_session: Session):
    """§15's bulk delete removes the business; a ledger row pointing at nothing
    cannot be rendered, so it cascades rather than being left dangling."""
    run = _run(db_session)
    business = _lead(db_session, run, "A", 90)

    extract(db_session, ResultQuery(run_ids=(run.id,)), SMALLEST)
    db_session.delete(business)
    db_session.flush()

    assert list_extractions(db_session) == []

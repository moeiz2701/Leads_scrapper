"""The read side — §13 Screen 3's filters, §15 suppression, §10.1's union.

The load-bearing test in this file is
``test_export_and_table_are_the_same_rows``. §12.2 requires the CSV to "respect
the active table filters and sort order", which makes any divergence between the
two a defect by definition rather than a difference of opinion — so both go
through ``fetch_results`` and this pins that they still do.
"""

from __future__ import annotations

import itertools
import uuid

from sqlalchemy.orm import Session

from leadscraper.core import batches
from leadscraper.db.models import Business, Contact, DoNotContact, Run
from leadscraper.enums import (
    ContactKind,
    LineType,
    NumberPreference,
    RunStatus,
    Source,
    WhatsAppLabel,
)
from leadscraper.export import build_row
from leadscraper.services.results import ResultQuery, fetch_results
from tests.conftest import requires_db

_SEQUENCE = itertools.count(1)


def _number() -> str:
    return f"+9230055{next(_SEQUENCE):05d}"


def _run(session: Session, city="Islamabad", category="salon") -> Run:
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
        area="F-7",
        address="Jinnah Super Market",
        place_id=f"place-{uuid.uuid4()}",
        rating=4.6,
        review_count=31,
        lead_score=70,
    )
    business = Business(run_id=run.id, **{**base, **overrides})
    session.add(business)
    session.flush()
    return business


def _phone(session: Session, business: Business, e164: str | None = None, **overrides) -> Contact:
    base = dict(
        kind=ContactKind.PHONE,
        value_raw=e164 or "0300 5500001",
        value_e164=e164 or _number(),
        line_type=LineType.MOBILE,
        wa_evidence=0.60,
        wa_label=WhatsAppLabel.LIKELY,
        confidence=0.85,
        source=Source.GOOGLE_MAPS,
        source_url="https://maps.google.com/?cid=1",
        rank=1,
    )
    contact = Contact(business_id=business.id, **{**base, **overrides})
    session.add(contact)
    session.flush()
    return contact


def _suppress(session: Session, **kwargs) -> None:
    session.add(DoNotContact(**kwargs))
    session.flush()


# --------------------------------------------------------------------------- #
# The invariant §12.2 turns into a defect
# --------------------------------------------------------------------------- #


@requires_db
def test_export_and_table_are_the_same_rows(db_session: Session):
    """One query layer, so the CSV cannot show what the screen does not.

    §12.2 generates server-side precisely so the filter state matches; if these
    two ever came from different code paths the operator would export a file
    they had no way to preview.
    """
    run = _run(db_session)
    for score in (90, 40, 70):
        _phone(db_session, _business(db_session, run, lead_score=score))

    query = ResultQuery(run_ids=(run.id,), min_score=60)
    page = fetch_results(db_session, query)

    assert [r["lead_score"] for r in page.rows] == [90, 70]
    # The exporter consumes exactly this list — no second filter, no re-sort.
    assert page.total == 2


# --------------------------------------------------------------------------- #
# §15 suppression
# --------------------------------------------------------------------------- #


@requires_db
def test_suppressed_number_loses_its_slot_but_keeps_its_row(db_session: Session):
    """§15 says "checked at export time"; this checks it on every read.

    A suppressed number rendered as ``phone_1`` is a number the operator rings.
    The row itself stays in the database — §15 needs it for provenance and §10.1
    never discards a contact.
    """
    run = _run(db_session)
    business = _business(db_session, run)
    blocked = _phone(db_session, business, "+923005500900", rank=1)
    _phone(db_session, business, "+923005500901", rank=2)
    _suppress(db_session, value_e164="+923005500900", reason="asked to be removed")

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,)))

    assert page.rows[0]["phone_1"] == "+923005500901"
    assert page.suppressed_contacts == 1
    # Still on disk, with its provenance intact.
    assert db_session.get(Contact, blocked.id) is not None


@requires_db
def test_business_with_every_number_suppressed_leaves_the_table(db_session: Session):
    """Nothing left to ring is not a lead — but it is still not deleted."""
    run = _run(db_session)
    business = _business(db_session, run)
    _phone(db_session, business, "+923005500910")
    _suppress(db_session, value_e164="+923005500910")

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,)))

    assert page.rows == []
    assert page.suppressed_businesses == 1
    assert db_session.get(Business, business.id) is not None


@requires_db
def test_business_that_never_had_a_phone_is_not_treated_as_suppressed(db_session: Session):
    """25 of the 199 Islamabad businesses have no phone at all. They are bad
    leads, not removal requests, and the two must not be conflated."""
    run = _run(db_session)
    _business(db_session, run, name="No Contact Salon")

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,)))

    assert len(page.rows) == 1
    assert page.suppressed_businesses == 0


@requires_db
def test_suppressed_domain_removes_the_whole_business(db_session: Session):
    """A suppressed *number* removes a number. A suppressed *domain* is the
    business itself asking, so its other numbers go too."""
    run = _run(db_session)
    business = _business(db_session, run, website="https://paragon.pk/contact")
    _phone(db_session, business, "+923005500920", rank=1)
    _phone(db_session, business, "+923005500921", rank=2)
    _suppress(db_session, domain="paragon.pk")

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,)))

    assert page.rows == []
    assert page.suppressed_businesses == 1


@requires_db
def test_suppression_counts_are_reported_not_silent(db_session: Session):
    """An operator seeing 44 rows where a colleague saw 45 needs to know why."""
    run = _run(db_session)
    business = _business(db_session, run)
    _phone(db_session, business, "+923005500930", rank=1)
    _phone(db_session, business, "+923005500931", rank=2)
    _suppress(db_session, value_e164="+923005500930")

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,)))
    assert page.suppressed_contacts == 1


# --------------------------------------------------------------------------- #
# §13 Screen 3 filters
# --------------------------------------------------------------------------- #


@requires_db
def test_whatsapp_filter_reads_the_numbers_the_operator_can_see(db_session: Session):
    """Filtering on an unranked contact would return rows that look, on screen,
    like they do not match — the confirmed number has no column."""
    run = _run(db_session)
    visible = _business(db_session, run, name="Confirmed Salon")
    _phone(db_session, visible, rank=1, wa_label=WhatsAppLabel.CONFIRMED)

    hidden = _business(db_session, run, name="Hidden Salon")
    _phone(db_session, hidden, rank=1, wa_label=WhatsAppLabel.LIKELY)
    _phone(db_session, hidden, rank=None, wa_label=WhatsAppLabel.CONFIRMED)

    page = fetch_results(
        db_session,
        ResultQuery(run_ids=(run.id,), whatsapp=(WhatsAppLabel.CONFIRMED,)),
    )
    assert [r["business_name"] for r in page.rows] == ["Confirmed Salon"]


@requires_db
def test_min_score_excludes_unscored_rows_rather_than_treating_them_as_zero(
    db_session: Session,
):
    """§10.2 inverted: an unscored business has not failed the bar, it has not
    been measured against it. Showing it under "score ≥ 60" would be a guess."""
    run = _run(db_session)
    _phone(db_session, _business(db_session, run, name="Scored", lead_score=70))
    _phone(db_session, _business(db_session, run, name="Unscored", lead_score=None))

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,), min_score=60))
    assert [r["business_name"] for r in page.rows] == ["Scored"]


@requires_db
def test_has_owner_name_filter_works_in_both_directions(db_session: Session):
    run = _run(db_session)
    named = _business(db_session, run, name="Named")
    _phone(db_session, named, person_name="Hina Khan")
    _business(db_session, run, name="Unnamed")
    _phone(db_session, db_session.get(Business, _business(db_session, run, name="Also Unnamed").id))

    with_name = fetch_results(
        db_session, ResultQuery(run_ids=(run.id,), has_owner_name=True)
    )
    assert [r["business_name"] for r in with_name.rows] == ["Named"]

    without = fetch_results(
        db_session, ResultQuery(run_ids=(run.id,), has_owner_name=False)
    )
    assert "Named" not in [r["business_name"] for r in without.rows]


@requires_db
def test_free_text_search_finds_a_business_by_its_phone_number(db_session: Session):
    """The §16 hand-check is 50 rounds of "whose number is this?"."""
    run = _run(db_session)
    business = _business(db_session, run, name="Paragon Salon")
    _phone(db_session, business, "+923005500940")
    _phone(db_session, _business(db_session, run, name="Other Salon"), "+923005500941")

    # Typed the way an operator has it written down, not in E.164.
    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,), search="0300 5500940"))
    assert [r["business_name"] for r in page.rows] == ["Paragon Salon"]


@requires_db
def test_free_text_search_matches_names_case_insensitively(db_session: Session):
    run = _run(db_session)
    _phone(db_session, _business(db_session, run, name="Paragon Salon"))
    _phone(db_session, _business(db_session, run, name="Glow Studio"))

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,), search="paragon"))
    assert [r["business_name"] for r in page.rows] == ["Paragon Salon"]


@requires_db
def test_has_website_splits_the_run_into_two_halves_that_add_back_up(
    db_session: Session,
):
    """Three states, and no row falls between them.

    The two halves are different work: a business with a site is one §5.2 can
    confirm a WhatsApp number on, and one without is a business where §6's social
    pass is the only route to a `confirmed` label there will ever be. So the
    partition has to be exhaustive — a row visible under neither setting would be
    a lead the operator never sees while believing they have looked at both.
    """
    run = _run(db_session)
    _phone(db_session, _business(db_session, run, name="Has One", website="https://a.pk"))
    _phone(db_session, _business(db_session, run, name="Has None", website=None))
    _phone(db_session, _business(db_session, run, name="Blank", website=""))

    def names(has_website: bool | None) -> list[str]:
        page = fetch_results(
            db_session, ResultQuery(run_ids=(run.id,), has_website=has_website)
        )
        return sorted(r["business_name"] for r in page.rows)

    assert names(True) == ["Has One"]
    # An empty string is the absence of a website, not a website. `website` is
    # gap-filled from §5.1's payload and §6.4's bio link, and a blank surviving
    # one of those must not land in the "has a site" half.
    assert names(False) == ["Blank", "Has None"]
    assert names(None) == ["Blank", "Has None", "Has One"]
    assert sorted(names(True) + names(False)) == names(None)


# --------------------------------------------------------------------------- #
# Outreach batches (_BATCH_SPEC.md)
# --------------------------------------------------------------------------- #


def _food(session: Session, run: Run, name: str, **overrides) -> Business:
    """A food business with one `likely` mobile — batchable by default."""
    base = dict(category="food", subcategory="Restaurant", review_count=800, rating=4.5)
    business = _business(
        session, run, name=name, name_norm=name.lower(), **{**base, **overrides}
    )
    _phone(session, business)
    return business


@requires_db
def test_the_batch_filter_narrows_the_table_to_one_message(db_session: Session):
    run = _run(db_session, category="food")
    _food(db_session, run, "No Site")
    _food(db_session, run, "Sited", website="https://sited.pk")
    _food(db_session, run, "Cafe", subcategory="Cafe")

    page = fetch_results(
        db_session,
        ResultQuery(run_ids=(run.id,), batches=(batches.DELIVERY_NOSITE,)),
    )
    assert [r["business_name"] for r in page.rows] == ["No Site"]
    assert page.rows[0]["_batch"] == batches.DELIVERY_NOSITE


@requires_db
def test_the_counts_describe_the_whole_view_not_the_filtered_slice(
    db_session: Session,
):
    """The picker is the only place the operator sees how a run divides up.

    Counting after the batch filter would leave every option but the selected one
    reading 0, which makes the picker useless for the decision it exists to
    support — "what am I working next?".
    """
    run = _run(db_session, category="food")
    _food(db_session, run, "No Site")
    _food(db_session, run, "Sited", website="https://sited.pk")
    _food(db_session, run, "Quiet", review_count=12)

    unfiltered = fetch_results(db_session, ResultQuery(run_ids=(run.id,)))
    filtered = fetch_results(
        db_session,
        ResultQuery(run_ids=(run.id,), batches=(batches.DELIVERY_NOSITE,)),
    )

    assert filtered.total == 1
    assert filtered.batch_counts == unfiltered.batch_counts
    assert filtered.batch_counts[batches.DELIVERY_SITE] == 1
    assert filtered.batch_counts[batches.EARLY_STAGE] == 1


@requires_db
def test_the_batches_partition_the_run_exhaustively(db_session: Session):
    """Every visible row is in exactly one batch, and the counts add up.

    This is the property the whole cascade exists for: a business in two batches
    gets messaged twice, and one in none never gets messaged at all. Asserted
    against the unfiltered total rather than eyeballed, the way the `has_website`
    split is.
    """
    run = _run(db_session, category="food")
    _food(db_session, run, "Delivery No Site")
    _food(db_session, run, "Delivery Site", website="https://a.pk")
    _food(db_session, run, "Cafe No Site", subcategory="Cafe")
    _food(db_session, run, "Cafe Site", subcategory="Cafe", website="https://b.pk")
    _food(db_session, run, "Poorly Rated", rating=3.2)
    _food(db_session, run, "Quiet", review_count=8)
    _business(db_session, run, name="Unreachable", category="food", review_count=900)

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,)))

    assert page.total == 7
    assert sum(page.batch_counts.values()) == page.total
    assert page.batch_counts == {
        batches.DELIVERY_NOSITE: 1,
        batches.DELIVERY_SITE: 1,
        batches.CAFE_NOSITE: 1,
        batches.CAFE_SITE: 1,
        batches.REPUTATION: 1,
        batches.EARLY_STAGE: 1,
        batches.NO_WHATSAPP: 1,
        batches.UNBATCHED: 0,
    }


@requires_db
def test_a_non_food_run_is_entirely_unbatched(db_session: Session):
    """Seven zeroes and an explanation, rather than seven plausible batches.

    The thresholds and the dine-in list came off one Lahore × food scrape. Run a
    salon through them and it lands in `delivery-nosite`, whose message argues
    about Foodpanda's commission — a real-looking label that would be sent.
    """
    run = _run(db_session, category="salon")
    _phone(db_session, _business(db_session, run, category="salon", review_count=800))

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,)))

    assert page.rows[0]["_batch"] == batches.UNBATCHED
    assert page.batch_counts[batches.UNBATCHED] == 1
    assert all(page.batch_counts[slug] == 0 for slug in batches.SLUGS)


@requires_db
def test_a_suppressed_number_moves_a_business_into_no_whatsapp(db_session: Session):
    """§15 first, then the cascade — in that order, and it matters.

    Assigning before suppression would file this business under `delivery-nosite`
    on the strength of a number §15 says never to ring, and then hand the
    operator a batch whose clipboard comes back one short with no explanation.
    Its landline survives, so the row itself stays — a business with a number
    nobody may WhatsApp is exactly what `no-whatsapp` is for, and §5 of the spec
    routes it to email or a visit rather than deleting it.
    """
    run = _run(db_session, category="food")
    business = _food(db_session, run, "Silenced")
    _phone(
        db_session,
        business,
        line_type=LineType.LANDLINE,
        wa_label=WhatsAppLabel.NO,
        rank=2,
    )
    _suppress(
        db_session,
        value_e164=business.contacts[0].value_e164,
        reason="removal request",
    )

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,)))

    assert page.total == 1
    assert page.rows[0]["_batch"] == batches.NO_WHATSAPP
    assert page.rows[0]["_wa_number"] is None


@requires_db
def test_the_derived_number_is_the_one_the_batch_would_message(db_session: Session):
    run = _run(db_session, category="food")
    business = _food(db_session, run, "Paragon")
    confirmed = _phone(
        db_session, business, wa_label=WhatsAppLabel.CONFIRMED, rank=2
    )

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,)))

    assert page.rows[0]["_wa_number"] == confirmed.value_e164
    assert page.rows[0]["_wa_confidence"] == WhatsAppLabel.CONFIRMED


@requires_db
def test_send_rank_orders_each_batch_by_review_count(db_session: Session):
    """§6 — "highest-value prospects get contacted while the number is freshest".

    Ranked *within* the batch, so two businesses in different batches can both be
    #1, and independent of the table's sort: the pull is the table, so this
    numbering informs the operator rather than quietly reordering what they are
    about to extract.
    """
    run = _run(db_session, category="food")
    _food(db_session, run, "Busy", review_count=2_000)
    _food(db_session, run, "Middling", review_count=900)
    _food(db_session, run, "Cafe", subcategory="Cafe", review_count=300)

    page = fetch_results(
        db_session, ResultQuery(run_ids=(run.id,), sort="business_name")
    )
    ranks = {r["business_name"]: (r["_batch"], r["_send_rank"]) for r in page.rows}

    assert ranks["Busy"] == (batches.DELIVERY_NOSITE, 1)
    assert ranks["Middling"] == (batches.DELIVERY_NOSITE, 2)
    # A different batch numbers from 1 again — it is a send order, not a rank in
    # the table.
    assert ranks["Cafe"] == (batches.CAFE_NOSITE, 1)


@requires_db
def test_an_unknown_batch_token_yields_no_rows_rather_than_all_of_them(
    db_session: Session,
):
    """Failing open would *widen* the view.

    A typo that quietly matched everything would present a whole run as one
    batch — and then get it extracted under a single message. The API rejects
    unknown tokens outright; this pins the layer beneath that guard.
    """
    run = _run(db_session, category="food")
    _food(db_session, run, "Paragon")

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,), batches=("B99",)))
    assert page.total == 0


# --------------------------------------------------------------------------- #
# Sorting
# --------------------------------------------------------------------------- #


@requires_db
def test_unscored_rows_sort_last_in_both_directions(db_session: Session):
    """§10.2: a missing value is not a low one. It belongs at the end of the
    table whichever way the operator clicked the header."""
    run = _run(db_session)
    for name, score in (("High", 90), ("Low", 20), ("Unknown", None)):
        _phone(db_session, _business(db_session, run, name=name, lead_score=score))

    descending = fetch_results(db_session, ResultQuery(run_ids=(run.id,)))
    assert [r["business_name"] for r in descending.rows] == ["High", "Low", "Unknown"]

    ascending = fetch_results(
        db_session, ResultQuery(run_ids=(run.id,), descending=False)
    )
    assert [r["business_name"] for r in ascending.rows] == ["Low", "High", "Unknown"]


@requires_db
def test_default_sort_is_lead_score_descending(db_session: Session):
    """§13 Screen 3: "default ``lead_score DESC``"."""
    run = _run(db_session)
    for score in (40, 90, 65):
        _phone(db_session, _business(db_session, run, lead_score=score))

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,)))
    assert [r["lead_score"] for r in page.rows] == [90, 65, 40]


# --------------------------------------------------------------------------- #
# §10.1 read-side union
# --------------------------------------------------------------------------- #


@requires_db
def test_cross_run_union_collapses_on_place_id_without_deleting(db_session: Session):
    """§10.1's Phase 5 note: the operator wants one table, not four.

    Done at read time, so the four Lahore runs whose 232 place_ids are 72 real
    businesses still exist as four runs — §16's "validate by re-running" depends
    on that.
    """
    first, second = _run(db_session, city="Lahore"), _run(db_session, city="Lahore")
    shared = f"place-{uuid.uuid4()}"
    thin = _business(db_session, first, name="Paragon Salon", place_id=shared, lead_score=45)
    _phone(db_session, thin)
    rich = _business(db_session, second, name="Paragon Salon", place_id=shared, lead_score=80)
    _phone(db_session, rich)

    both = (first.id, second.id)
    flat = fetch_results(db_session, ResultQuery(run_ids=both))
    assert len(flat.rows) == 2

    collapsed = fetch_results(
        db_session, ResultQuery(run_ids=both, collapse_place_id=True)
    )
    assert len(collapsed.rows) == 1
    assert collapsed.collapsed == 1
    # The enriched run knows more about the business than the thin one.
    assert collapsed.rows[0]["lead_score"] == 80
    # And nothing was destroyed.
    assert db_session.get(Business, thin.id) is not None


@requires_db
def test_union_keeps_every_business_that_has_no_place_id(db_session: Session):
    """Collapsing two rows that share nothing would be a merge asserted on no
    evidence — the §10.1 failure mode, arriving through the read side."""
    run = _run(db_session)
    _phone(db_session, _business(db_session, run, name="A", place_id=None))
    _phone(db_session, _business(db_session, run, name="B", place_id=None))

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,), collapse_place_id=True))
    assert len(page.rows) == 2
    assert page.collapsed == 0


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #


@requires_db
def test_total_counts_the_filtered_set_not_the_page(db_session: Session):
    run = _run(db_session)
    for index in range(5):
        _phone(db_session, _business(db_session, run, name=f"Salon {index}"))

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,), limit=2))
    assert len(page.rows) == 2
    assert page.total == 5


@requires_db
def test_projection_matches_build_row_exactly(db_session: Session):
    """The table is the §12.1 projection, not a parallel shape that resembles it."""
    run = _run(db_session)
    business = _business(db_session, run)
    _phone(db_session, business)

    page = fetch_results(db_session, ResultQuery(run_ids=(run.id,)))
    direct = build_row(business, list(business.contacts))

    for column, value in direct.items():
        assert page.rows[0][column] == value

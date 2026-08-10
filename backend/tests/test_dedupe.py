"""Stage 6 — the §10.1 dedupe cascade.

Most of this file exists to pin one measured correction: **§10.1's phone and
domain tiers destroy leads when applied as written.** The cases below are real
rows from the two live runs, not invented ones, because the argument for the
departure is empirical and a reader should be able to check it.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime

from rapidfuzz.fuzz import token_set_ratio
from sqlalchemy import select
from sqlalchemy.orm import Session

from leadscraper.core.textnorm import conflicting_segments, normalise_name
from leadscraper.db.models import Business, Contact, Run
from leadscraper.enums import (
    BelongsTo,
    ContactKind,
    LineType,
    NumberPreference,
    RunStatus,
    Source,
    WhatsAppLabel,
)
from leadscraper.services.dedupe import MatchTier, Verdict, decide, dedupe_run
from tests.conftest import requires_db

FUZZY = 88
CORROBORATED = 75

_SEQUENCE = itertools.count(1)


def _number() -> str:
    return f"+9230012{next(_SEQUENCE):05d}"


def _run(session: Session) -> Run:
    run = Run(
        city="Islamabad",
        category="salon",
        number_pref=NumberPreference.OWNER_FIRST,
        sources_enabled={"google_maps": True},
        status=RunStatus.RUNNING,
    )
    session.add(run)
    session.flush()
    return run


def _business(session: Session, run: Run, name: str, **overrides) -> Business:
    base = dict(
        name=name,
        name_norm=normalise_name(name),
        city="Islamabad",
        area="F-7",
        address="Jinnah Super Market, F-7 Markaz",
        lat=33.7167,
        lng=73.0552,
        place_id=f"place-{uuid.uuid4()}",
        rating=4.6,
        review_count=31,
    )
    business = Business(run_id=run.id, **{**base, **overrides})
    session.add(business)
    session.flush()
    return business


def _phone(session: Session, business: Business, e164: str, **overrides) -> Contact:
    base = dict(
        line_type=LineType.MOBILE,
        wa_evidence=0.60,
        wa_label=WhatsAppLabel.LIKELY,
        confidence=0.85,
        source=Source.GOOGLE_MAPS,
        source_url="https://www.google.com/maps",
    )
    contact = Contact(
        business_id=business.id,
        kind=ContactKind.PHONE,
        value_raw=e164,
        value_e164=e164,
        belongs_to=BelongsTo.BUSINESS,
        scraped_at=datetime.now(UTC),
        **{**base, **overrides},
    )
    business.contacts.append(contact)
    session.flush()
    return contact


def _decide(left: Business, right: Business, tier: MatchTier) -> Verdict:
    return decide(
        left, right, tier, fuzzy_threshold=FUZZY, corroborated_threshold=CORROBORATED
    )


# --------------------------------------------------------------------------- #
# The measured correction to §10.1
# --------------------------------------------------------------------------- #


@requires_db
def test_a_chain_sharing_a_number_is_not_one_business(db_session: Session) -> None:
    """§10.1 tier 1 says "normalised E.164 phone match → same business". Applied
    literally to real data it merges every multi-branch chain into one row.

    House of Salons publishes the same seven numbers on three Islamabad branches
    — F-7 Female Studio, F-7 Men's Salon, F-10 — 171 m to 5.4 km apart. Merging
    them loses two contactable premises with two separate addresses.

    Measured across both live runs: 36 groups of businesses share a number and
    **not one of them is a duplicate.**
    """
    run = _run(db_session)
    f7_female = _business(db_session, run, "House of Salons - F7 (Female Studio)",
                          lat=33.7167, lng=73.0552)
    f10 = _business(db_session, run, "House of Salons - F10", lat=33.6900, lng=73.0130)

    assert _decide(f7_female, f10, MatchTier.EXACT_PHONE) is Verdict.REJECTED_DISTANCE

    for business in (f7_female, f10):
        _phone(db_session, business, "+923000395761")
        _phone(db_session, business, "+923270111104")

    report = dedupe_run(db_session, run)
    assert report.businesses_after == 2
    assert report.businesses_absorbed == 0
    assert report.rejected_distance >= 1


@requires_db
def test_a_chain_sharing_a_domain_is_not_one_business(db_session: Session) -> None:
    """§10.1 tier 4, same problem. All five shared domains in the Lahore run are
    chains: Shelby's & Co. Johar Town and DHA sit 13.3 km apart on one site."""
    run = _run(db_session)
    johar = _business(db_session, run, "Shelby's & Co. Johar Town",
                      website="https://shelbysandco.com", lat=31.4600, lng=74.2700)
    dha = _business(db_session, run, "Shelby's & Co. DHA",
                    website="https://shelbysandco.com/", lat=31.4750, lng=74.4050)

    assert _decide(johar, dha, MatchTier.DOMAIN) is Verdict.REJECTED_DISTANCE

    report = dedupe_run(db_session, run)
    assert report.businesses_after == 2
    assert report.rejected_distance >= 1


@requires_db
def test_two_shops_in_one_plaza_sharing_a_landline_stay_separate(
    db_session: Session,
) -> None:
    """The other half of the correction. Distance alone is not enough — Spanish
    Club and a massage centre share a mobile 16 m apart in the same mall, and
    Naveeds Salon and Nauman's Hair Saloon share one 40 m apart. Both pairs pass
    the geo test and are still two businesses, so the name bar has to hold.

    Measured ceiling: among businesses within 150 m that share a number, the
    highest name similarity in either run is **54.5**. That is why the
    corroborated threshold must stay well above it.
    """
    run = _run(db_session)
    naveeds = _business(db_session, run, "Naveeds Salon", lat=33.71670, lng=73.05520)
    naumans = _business(db_session, run, "Nauman's Hair Saloon",
                        lat=33.71700, lng=73.05545)

    assert token_set_ratio(naveeds.name_norm, naumans.name_norm) < CORROBORATED
    assert _decide(naveeds, naumans, MatchTier.EXACT_PHONE) is Verdict.REJECTED_NAME

    _phone(db_session, naveeds, "+923335492282")
    _phone(db_session, naumans, "+923335492282")

    report = dedupe_run(db_session, run)
    assert report.businesses_after == 2
    assert report.rejected_name >= 1


def test_the_corroborated_threshold_clears_the_measured_ceiling() -> None:
    """The three real within-150 m shared-number pairs, with their measured name
    similarities. All three are distinct businesses, so the threshold must sit
    above the highest of them — dropping it below 55 re-introduces the bug."""
    measured = [
        ("Spanish Club", "Massage Center ( Best Massage In Islamabad)", 27.3),
        ("Nisar's Beauty Salon", "Makeover Saloon", 45.7),
        ("Naveeds Salon", "Nauman's Hair Saloon", 54.5),
    ]
    for left, right, expected in measured:
        ratio = token_set_ratio(normalise_name(left), normalise_name(right))
        assert abs(ratio - expected) < 1.5, f"{left} <> {right} moved to {ratio}"
        assert ratio < CORROBORATED


@requires_db
def test_place_id_merges_without_needing_corroboration(db_session: Session) -> None:
    """Tier 2 is Google asserting identity, not a similarity — it stands alone.

    Structurally inert *within* a run today, because ``place_id`` is unique per
    run (README departure #1) so two rows cannot share one. It is implemented for
    the sources that insert businesses without going through Maps ingest: §3.2
    seed rows and the Phase 6 directory modules.
    """
    run = _run(db_session)
    a = _business(db_session, run, "Paragon Salon", place_id=None)
    b = _business(db_session, run, "Totally Different Name", place_id=None,
                  lat=31.5, lng=74.3)
    assert _decide(a, b, MatchTier.PLACE_ID) is Verdict.MERGE


@requires_db
def test_a_mens_and_a_womens_branch_at_one_address_stay_separate(
    db_session: Session,
) -> None:
    """From the live Karachi run, and the reason ``conflicting_segments`` exists.

    "Lavish Women Salon DHA Branch" and "Lavish Men's Salon Dha Branch" share a
    domain, sit **3 m apart**, and score **93.1** — past even the strict 88
    threshold, because token-set ratio barely moves for one differing token in an
    otherwise identical name. They are two separately-staffed premises with two
    numbers, and §4.2's salon synonyms already list "men's salon" and "ladies
    salon" as different queries.
    """
    run = _run(db_session)
    women = _business(db_session, run, "Lavish Women Salon DHA Branch",
                      website="https://lavishsaloon.com.pk",
                      lat=24.8092915, lng=67.0693872)
    men = _business(db_session, run, "Lavish Men's Salon Dha Branch",
                    website="https://lavishsaloon.com.pk",
                    lat=24.8093065, lng=67.0694058)

    # The ratio really does clear the strict bar — the guard is load-bearing.
    assert token_set_ratio(women.name_norm, men.name_norm) > FUZZY

    assert _decide(women, men, MatchTier.DOMAIN) is Verdict.REJECTED_SEGMENT
    assert _decide(women, men, MatchTier.FUZZY_NAME_GEO) is Verdict.REJECTED_SEGMENT

    _phone(db_session, women, "+923323238540")
    _phone(db_session, men, "+923329055590")

    report = dedupe_run(db_session, run)
    assert report.businesses_after == 2
    assert report.rejected_segment >= 1


def test_a_segment_conflict_needs_both_names_to_declare_one() -> None:
    """Conservative by design: a barber shop next to an unlabelled salon is not a
    conflict, because only one of them said anything about its clientele."""
    assert conflicting_segments(normalise_name("Lavish Men's Salon"),
                                normalise_name("Lavish Women Salon"))
    assert conflicting_segments(normalise_name("Ali Barber Shop"),
                                normalise_name("Ali Ladies Parlour"))
    assert conflicting_segments(normalise_name("Cut Kids Salon"),
                                normalise_name("Cut Gents Salon"))

    assert not conflicting_segments(normalise_name("Lavish Salon"),
                                    normalise_name("Lavish Men's Salon"))
    assert not conflicting_segments(normalise_name("Paragon Salon"),
                                    normalise_name("Paragon Salon"))
    assert not conflicting_segments(normalise_name("Ali Mens Salon"),
                                    normalise_name("Ali Gents Salon"))


# --------------------------------------------------------------------------- #
# The fuzzy tier, which is the one that survived unchanged
# --------------------------------------------------------------------------- #


@requires_db
def test_the_same_salon_found_twice_nearby_collapses(db_session: Session) -> None:
    """§10.1 tier 3 as written: token-set ratio ≥ 88 AND < 150 m."""
    run = _run(db_session)
    first = _business(db_session, run, "Paragon Salon", lat=33.71670, lng=73.05520)
    second = _business(db_session, run, "Paragon Salon!", lat=33.71675, lng=73.05525)
    _phone(db_session, first, "+923001234567")
    _phone(db_session, second, "+923339876543")

    report = dedupe_run(db_session, run)

    assert report.businesses_after == 1
    assert report.businesses_absorbed == 1
    assert report.merges_by_tier.get(MatchTier.FUZZY_NAME_GEO) == 1


@requires_db
def test_the_same_name_in_two_areas_does_not_collapse(db_session: Session) -> None:
    """"A false merge silently destroys a lead while a false split only costs a
    duplicate row" — Paragon Gulberg and Paragon DHA are two salons."""
    run = _run(db_session)
    _business(db_session, run, "Paragon Salon", lat=31.5204, lng=74.3587)
    _business(db_session, run, "Paragon Salon", lat=31.4750, lng=74.4050)

    report = dedupe_run(db_session, run)
    assert report.businesses_after == 2


@requires_db
def test_a_business_without_coordinates_cannot_fuzzy_match(db_session: Session) -> None:
    """§10.1's tier 3 is an AND. Missing coordinates means the distance test
    cannot pass, and guessing "probably the same place" is how a merge destroys
    a lead."""
    run = _run(db_session)
    _business(db_session, run, "Paragon Salon", lat=None, lng=None)
    _business(db_session, run, "Paragon Salon", lat=None, lng=None)

    report = dedupe_run(db_session, run)
    assert report.businesses_after == 2


# --------------------------------------------------------------------------- #
# Merge behaviour
# --------------------------------------------------------------------------- #


@requires_db
def test_a_merge_never_discards_a_contact(db_session: Session) -> None:
    """§10.1: "union all contacts … a second number is a second column, and
    that's exactly what you asked for.\""""
    run = _run(db_session)
    first = _business(db_session, run, "Paragon Salon", lat=33.71670, lng=73.05520)
    second = _business(db_session, run, "Paragon Salon", lat=33.71675, lng=73.05525)
    _phone(db_session, first, "+923001234567")
    _phone(db_session, second, "+923339876543")
    _phone(db_session, second, "+924232294007", line_type=LineType.LANDLINE,
           wa_evidence=0.0, wa_label=WhatsAppLabel.NO)

    dedupe_run(db_session, run)

    survivor = db_session.execute(select(Business)).scalar_one()
    assert {c.value_e164 for c in survivor.contacts} == {
        "+923001234567", "+923339876543", "+924232294007",
    }


@requires_db
def test_a_merge_keeps_both_provenance_rows_for_one_number(db_session: Session) -> None:
    """Two sources carrying the same number are two records, and §1 says every
    record keeps the URL it came from. Folding them would drop whichever
    ``source`` lost — which is the input §10.2's source_agreement counts.

    The operator is not shown the number twice: §3.3 ranking gives the export
    slot to one row and leaves the other unranked.
    """
    run = _run(db_session)
    first = _business(db_session, run, "Paragon Salon", lat=33.71670, lng=73.05520)
    second = _business(db_session, run, "Paragon Salon", lat=33.71675, lng=73.05525)
    _phone(db_session, first, "+923001234567", source=Source.GOOGLE_MAPS)
    _phone(db_session, second, "+923001234567", source=Source.BUSINESS_WEBSITE,
           source_url="https://paragon.pk/contact", wa_evidence=1.00,
           wa_label=WhatsAppLabel.CONFIRMED)

    report = dedupe_run(db_session, run)
    survivor = db_session.execute(select(Business)).scalar_one()

    same_number = [c for c in survivor.contacts if c.value_e164 == "+923001234567"]
    assert len(same_number) == 2
    assert {c.source for c in same_number} == {
        Source.GOOGLE_MAPS, Source.BUSINESS_WEBSITE,
    }
    assert report.duplicate_numbers_after_merge == 1

    # Exactly one of them holds the export slot, and it is the proven one.
    ranked = [c for c in same_number if c.rank is not None]
    assert len(ranked) == 1
    assert ranked[0].wa_label == WhatsAppLabel.CONFIRMED


@requires_db
def test_a_merge_fills_gaps_and_never_overwrites(db_session: Session) -> None:
    run = _run(db_session)
    rich = _business(db_session, run, "Paragon Salon", lat=33.71670, lng=73.05520,
                     website="https://paragon.pk", review_count=400)
    thin = _business(db_session, run, "Paragon Salon", lat=33.71675, lng=73.05525,
                     website="https://other.pk", review_count=None,
                     instagram_url="https://instagram.com/paragon")
    _phone(db_session, rich, "+923001234567")
    _phone(db_session, rich, "+923339876543")
    _phone(db_session, thin, "+924232294007")

    dedupe_run(db_session, run)
    survivor = db_session.execute(select(Business)).scalar_one()

    assert survivor.website == "https://paragon.pk"   # not overwritten
    assert survivor.review_count == 400
    assert survivor.instagram_url == "https://instagram.com/paragon"  # gap filled


@requires_db
def test_a_merge_records_what_it_absorbed(db_session: Session) -> None:
    """``merged_from`` is the §10.1 bookkeeping — without it a merged row cannot
    be traced back, and §15's bulk-delete path needs to know what folded in."""
    run = _run(db_session)
    first = _business(db_session, run, "Paragon Salon", lat=33.71670, lng=73.05520)
    second = _business(db_session, run, "Paragon Salon", lat=33.71675, lng=73.05525)
    _phone(db_session, first, "+923001234567")
    absorbed_id = str(second.id)

    dedupe_run(db_session, run)
    survivor = db_session.execute(select(Business)).scalar_one()
    assert survivor.merged_from == [absorbed_id]


@requires_db
def test_merges_are_transitive(db_session: Session) -> None:
    """A ~ B and B ~ C means one business, not two merges leaving three rows."""
    run = _run(db_session)
    for offset in range(3):
        business = _business(db_session, run, "Paragon Salon",
                             lat=33.71670 + offset * 0.00002, lng=73.05520)
        _phone(db_session, business, _number())

    report = dedupe_run(db_session, run)
    assert report.businesses_after == 1
    assert report.groups_merged == 1
    assert report.businesses_absorbed == 2


@requires_db
def test_the_survivor_is_re_scored_after_absorbing(db_session: Session) -> None:
    """§2 puts scoring at Stage 5 and dedupe at Stage 6, so a merge lands after
    the scores. A survivor whose contact set just changed has a stale score
    unless the stage re-runs it."""
    run = _run(db_session)
    first = _business(db_session, run, "Paragon Salon", lat=33.71670, lng=73.05520)
    second = _business(db_session, run, "Paragon Salon", lat=33.71675, lng=73.05525)
    _phone(db_session, first, "+923001234567", source=Source.GOOGLE_MAPS)
    _phone(db_session, second, "+923339876543", source=Source.BUSINESS_WEBSITE,
           source_url="https://paragon.pk", wa_evidence=1.00,
           wa_label=WhatsAppLabel.CONFIRMED, confidence=0.95)

    report = dedupe_run(db_session, run)
    survivor = db_session.execute(select(Business)).scalar_one()

    assert report.rescored == 1
    assert survivor.lead_score is not None
    # Two sources now agree, and the confirmed number came with the merge.
    assert len({c.source for c in survivor.contacts}) == 2
    assert sorted(c.rank for c in survivor.contacts) == [1, 2]


@requires_db
def test_dedupe_is_idempotent(db_session: Session) -> None:
    run = _run(db_session)
    first = _business(db_session, run, "Paragon Salon", lat=33.71670, lng=73.05520)
    second = _business(db_session, run, "Paragon Salon", lat=33.71675, lng=73.05525)
    _phone(db_session, first, "+923001234567")
    _phone(db_session, second, "+923339876543")

    dedupe_run(db_session, run)
    survivor = db_session.execute(select(Business)).scalar_one()
    merged_from = list(survivor.merged_from or [])

    second_pass = dedupe_run(db_session, run)
    assert second_pass.businesses_absorbed == 0
    assert second_pass.businesses_after == 1
    survivor = db_session.execute(select(Business)).scalar_one()
    assert survivor.merged_from == merged_from


@requires_db
def test_dedupe_leaves_other_runs_alone(db_session: Session) -> None:
    """Scope decision, recorded in ``services/dedupe``: a business belongs to one
    run, so a cross-run merge has nowhere to put the survivor. The four Lahore ×
    salon runs in the live database overlap 232 place_ids down to 72, and
    collapsing them would be deleting three runs, not deduplicating a table."""
    run_a, run_b = _run(db_session), _run(db_session)
    for run in (run_a, run_b):
        business = _business(db_session, run, "Paragon Salon",
                             lat=33.71670, lng=73.05520)
        _phone(db_session, business, "+923001234567")

    report = dedupe_run(db_session, run_a)
    assert report.businesses_before == 1
    assert report.businesses_after == 1
    assert len(db_session.execute(select(Business)).scalars().all()) == 2


@requires_db
def test_a_run_with_one_business_is_a_no_op(db_session: Session) -> None:
    run = _run(db_session)
    _business(db_session, run, "Paragon Salon")
    report = dedupe_run(db_session, run)
    assert report.businesses_before == report.businesses_after == 1

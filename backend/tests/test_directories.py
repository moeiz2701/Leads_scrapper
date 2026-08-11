"""§5.3 directory corroboration — the join, and what it refuses to assert.

This is the first time §10.1's `fuzzy_name_geo` tier is asked to *find* a
duplicate rather than reject one. Every business in the database until now came
from Maps and carried a `place_id`, so tier 2 did all the work and tier 3 had
produced 1 merge against 1,027 rejections. A directory row has no `place_id`, so
name-and-distance is the only thing that can join it to the business it
describes — and the ways that can go wrong are the ways Phase 4 measured.

The matching tests build detached ``Business`` objects and need no database:
the rules are the thing worth pinning, and testing them against real rows would
only add a Postgres dependency to a pure decision.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from leadscraper.core.phone import normalise
from leadscraper.core.textnorm import normalise_name
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
from leadscraper.services.directories import (
    DIRECTORY_CONTACT_CONFIDENCE,
    Match,
    MatchTier,
    Refusal,
    _BusinessIndex,
    corroborate_run,
    match_listing,
)
from leadscraper.sources.businesslist import DirectoryHarvest, DirectoryListing
from tests.conftest import requires_db

FUZZY = 88
CORROBORATED = 75

# Two points ~40 m apart in Gulberg, and one 5 km away in DHA.
GULBERG = (31.5154147, 74.343849)
GULBERG_NEAR = (31.5157147, 74.343949)
DHA = (31.4798053, 74.3720544)


def _listing(
    name: str,
    *,
    lat: float | None = None,
    lng: float | None = None,
    phones: tuple[str, ...] = (),
    address: str | None = None,
    rating: float | None = None,
    review_count: int | None = None,
    directory_id: str = "1",
) -> DirectoryListing:
    parsed = tuple(p for p in (normalise(x) for x in phones) if p is not None)
    return DirectoryListing(
        name=name,
        name_norm=normalise_name(name),
        source_url=f"https://www.businesslist.pk/company/{directory_id}/x",
        directory_id=directory_id,
        address=address,
        phones=parsed,
        lat=lat,
        lng=lng,
        rating=rating,
        review_count=review_count,
    )


def _detached(
    name: str,
    *,
    lat: float | None = None,
    lng: float | None = None,
    phones: tuple[str, ...] = (),
) -> Business:
    business = Business(
        id=uuid.uuid4(),
        name=name,
        name_norm=normalise_name(name),
        lat=lat,
        lng=lng,
    )
    business.contacts = [
        Contact(
            kind=ContactKind.PHONE,
            value_raw=x,
            value_e164=normalise(x).e164,  # type: ignore[union-attr]
            source=Source.GOOGLE_MAPS,
            source_url="https://maps.google.com",
        )
        for x in phones
    ]
    return business


def _match(listing: DirectoryListing, *businesses: Business):
    return match_listing(
        listing,
        _BusinessIndex(list(businesses)),
        fuzzy_threshold=FUZZY,
        corroborated_threshold=CORROBORATED,
    )


# --------------------------------------------------------------------------- #
# Rule 1 — coordinates present: §10.1 tier 3, verbatim
# --------------------------------------------------------------------------- #


def test_a_located_row_joins_on_name_similarity_and_distance() -> None:
    """The tier §10.1 specified and Phase 4 left unchanged, finally doing the job
    the section said was "prospective"."""
    business = _detached("Pizza M21", lat=GULBERG[0], lng=GULBERG[1])
    result = _match(_listing("Pizza M21", lat=GULBERG_NEAR[0], lng=GULBERG_NEAR[1]), business)

    assert isinstance(result, Match)
    assert result.tier is MatchTier.FUZZY_NAME_GEO
    assert result.business_id == business.id


def test_a_similar_name_far_away_is_refused_on_distance() -> None:
    """A chain branch. Phase 4 measured that beyond 150 m several same-brand
    pairs score 100 and every one is a separate premises."""
    business = _detached("Pizza M21", lat=DHA[0], lng=DHA[1])
    result = _match(_listing("Pizza M21", lat=GULBERG[0], lng=GULBERG[1]), business)
    assert result is Refusal.REJECTED_DISTANCE


def test_a_different_business_at_the_same_address_is_refused_on_name() -> None:
    """Two shops in one plaza. §10.1's own measurement: within 150 m the highest
    name similarity between genuinely distinct businesses is 54.5."""
    business = _detached("Naveeds Salon", lat=GULBERG[0], lng=GULBERG[1])
    listing = _listing("Nauman's Hair Saloon", lat=GULBERG_NEAR[0], lng=GULBERG_NEAR[1])
    assert _match(listing, business) is Refusal.REJECTED_NAME


def test_a_clientele_conflict_is_refused_before_the_ratio_is_consulted() -> None:
    """"Lavish Women Salon" and "Lavish Men's Salon", 3 m apart, score 93.1 —
    past even the strict threshold. §10.1's segment rule is checked first here
    for the same reason ``dedupe.decide`` checks it first."""
    business = _detached("Lavish Men's Salon DHA Branch", lat=GULBERG[0], lng=GULBERG[1])
    result = _match(
        _listing("Lavish Women Salon DHA Branch", lat=GULBERG_NEAR[0], lng=GULBERG_NEAR[1]),
        business,
    )
    assert result is Refusal.REJECTED_SEGMENT


def test_the_best_scoring_candidate_wins_when_several_are_near() -> None:
    weak = _detached("Pizza Place", lat=GULBERG[0], lng=GULBERG[1])
    strong = _detached("Pizza M21", lat=GULBERG[0], lng=GULBERG[1])
    result = _match(_listing("Pizza M21", lat=GULBERG_NEAR[0], lng=GULBERG_NEAR[1]), weak, strong)

    assert isinstance(result, Match)
    assert result.business_id == strong.id


# --------------------------------------------------------------------------- #
# Rule 2 — a shared phone lowers the name bar but never waives distance
# --------------------------------------------------------------------------- #


def test_a_shared_phone_lowers_the_name_bar() -> None:
    """Phase 4's demotion, applied across sources: corroboration relaxes the
    name threshold from 88 to 75, and nothing else."""
    business = _detached(
        "Bombay Biryani Lower Mall",
        lat=GULBERG[0],
        lng=GULBERG[1],
        phones=("04237114000",),
    )
    listing = _listing(
        "Bombay Biryani", lat=GULBERG_NEAR[0], lng=GULBERG_NEAR[1], phones=("04237114000",)
    )
    result = _match(listing, business)

    assert isinstance(result, Match)
    assert result.tier is MatchTier.CORROBORATED_GEO


def test_a_shared_phone_still_does_not_waive_the_distance_test() -> None:
    """The single most important line of the Phase 4 correction. Applied
    literally, §10.1's phone tier merged 11 Islamabad and 7 Lahore businesses out
    of existence, each a contactable branch of a chain."""
    business = _detached("Bombay Biryani", lat=DHA[0], lng=DHA[1], phones=("04237114000",))
    listing = _listing("Bombay Biryani", lat=GULBERG[0], lng=GULBERG[1], phones=("04237114000",))
    assert _match(listing, business) is Refusal.REJECTED_DISTANCE


# --------------------------------------------------------------------------- #
# Rule 3 — no coordinates: distance is unavailable, so ambiguity refuses
# --------------------------------------------------------------------------- #


def test_an_unlocated_row_joins_on_a_unique_phone_match() -> None:
    """35% of BusinessList rows carry no ``data-ltd``. Refusing all of them
    strands a third of the source; this is the narrow rule that does not."""
    business = _detached("Bombay Biryani", lat=GULBERG[0], lng=GULBERG[1], phones=("04237114000",))
    result = _match(_listing("Bombay Biryani", phones=("04237114000",)), business)

    assert isinstance(result, Match)
    assert result.tier is MatchTier.PHONE_UNLOCATED


def test_an_unlocated_row_refuses_when_several_businesses_share_the_number() -> None:
    """The chain case, and the whole reason rule 3 is written the way it is.
    House of Salons publishes one number across three branches; with no
    coordinates there is nothing to say which branch the directory row describes,
    so it asserts nothing."""
    branches = [
        _detached("House of Salons F-7", lat=GULBERG[0], lng=GULBERG[1], phones=("0512345678",)),
        _detached("House of Salons F-10", lat=DHA[0], lng=DHA[1], phones=("0512345678",)),
    ]
    result = _match(_listing("House of Salons", phones=("0512345678",)), *branches)
    assert result is Refusal.AMBIGUOUS


def test_an_unlocated_row_with_no_phone_has_no_evidence_at_all() -> None:
    """Name alone is never enough. §10.1's tier 3 is an AND and this row can
    satisfy neither half."""
    business = _detached("Bombay Biryani", lat=GULBERG[0], lng=GULBERG[1])
    assert _match(_listing("Bombay Biryani"), business) is Refusal.NO_CANDIDATE


def test_an_unlocated_row_never_matches_a_business_by_name_alone() -> None:
    """The failure this rule exists to prevent: without the distance test, an
    identical name would join to any business anywhere in the city."""
    business = _detached("Bombay Biryani", lat=DHA[0], lng=DHA[1])
    assert _match(_listing("Bombay Biryani"), business) is Refusal.NO_CANDIDATE


def test_only_the_fuzzy_tier_can_match_without_a_shared_phone() -> None:
    """The structural reason Phase 6 measured zero yield, pinned so it cannot be
    quietly forgotten.

    Two of the three tiers — ``corroborated_geo`` and ``phone_unlocated`` —
    require an exact phone match to fire at all. So any business they match
    **already has the number the directory is offering**, and the join cannot add
    a contact by construction. Only ``fuzzy_name_geo`` can match a business whose
    numbers we do not already share, and it needs coordinates on both sides.

    Measured across four live slices: 333 listings, 7 matches, all of them
    phone-based, ``fuzzy_name_geo`` **0** — because BusinessList publishes
    geocoded approximations that miss §10.1's 150 m radius. Anyone widening that
    radius should read the §5.3 Phase 6 note first: at 500 m it admits one extra
    pair, and that pair is a false match.
    """
    business = _detached(
        "Pizza M21", lat=GULBERG[0], lng=GULBERG[1], phones=("04237114000",)
    )
    index = _BusinessIndex([business])

    # Same premises, name below the strict bar, no shared number → nothing to
    # lower the bar with, so no match. This is the shape a *useful* directory row
    # would have (a number we lack), and it is exactly the shape that cannot join.
    useful = _listing(
        "Pizza M 21 Gulberg Lahore",
        lat=GULBERG_NEAR[0],
        lng=GULBERG_NEAR[1],
        phones=("03216966621",),
    )
    result = match_listing(
        useful, index, fuzzy_threshold=FUZZY, corroborated_threshold=CORROBORATED
    )
    # 71.4 on token-set ratio — under the strict bar, and with no shared number
    # there is nothing to drop the bar to 75 either.
    assert result is Refusal.REJECTED_NAME

    # Give it a name that does clear tier 3's bar and the same row joins — and
    # only through this tier can a number Maps never had reach the business.
    named = _listing(
        "Pizza M21 Gulberg",
        lat=GULBERG_NEAR[0],
        lng=GULBERG_NEAR[1],
        phones=("03216966621",),
    )
    joined = match_listing(
        named, index, fuzzy_threshold=FUZZY, corroborated_threshold=CORROBORATED
    )
    assert isinstance(joined, Match)
    assert joined.tier is MatchTier.FUZZY_NAME_GEO


# --------------------------------------------------------------------------- #
# What a match does to the database
# --------------------------------------------------------------------------- #


class StubSource:
    """Hands back a fixed harvest, so the merge rules are testable without a
    network — the same shape ``test_enrichment`` uses for the website crawler."""

    def __init__(self, result: DirectoryHarvest) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def harvest(self, city, category) -> DirectoryHarvest:
        self.calls.append((city, category))
        return self.result


def _harvest(*listings: DirectoryListing, **overrides) -> DirectoryHarvest:
    return DirectoryHarvest(
        listings=list(listings),
        categories_requested=overrides.pop("categories_requested", 3),
        categories_answered=overrides.pop("categories_answered", 3),
        pages_fetched=overrides.pop("pages_fetched", 1),
        **overrides,
    )


def _run(session: Session) -> Run:
    run = Run(
        city="Lahore",
        category="food",
        number_pref=NumberPreference.OWNER_FIRST,
        sources_enabled={"google_maps": True, "directories": True},
        status=RunStatus.DONE,
    )
    session.add(run)
    session.flush()
    return run


def _saved(session: Session, run: Run, name: str, **kwargs) -> Business:
    phones = kwargs.pop("phones", ())
    business = Business(
        run_id=run.id, name=name, name_norm=normalise_name(name), city="Lahore", **kwargs
    )
    session.add(business)
    session.flush()
    for value in phones:
        parsed = normalise(value)
        session.add(
            Contact(
                business_id=business.id,
                kind=ContactKind.PHONE,
                value_raw=value,
                value_e164=parsed.e164,  # type: ignore[union-attr]
                line_type=parsed.line_type,  # type: ignore[union-attr]
                wa_evidence=0.60,
                wa_label=WhatsAppLabel.LIKELY,
                belongs_to=BelongsTo.BUSINESS,
                confidence=0.85,
                source=Source.GOOGLE_MAPS,
                source_url="https://maps.google.com",
                scraped_at=datetime.now(UTC),
            )
        )
    session.flush()
    session.refresh(business)
    return business


@requires_db
def test_a_directory_number_maps_never_had_becomes_a_new_contact(db_session: Session) -> None:
    """§10.1: never discard a contact. A second number is a second column."""
    run = _run(db_session)
    business = _saved(
        db_session, run, "Pizza M21", lat=GULBERG[0], lng=GULBERG[1], phones=("04237114000",)
    )
    listing = _listing(
        "Pizza M21",
        lat=GULBERG_NEAR[0],
        lng=GULBERG_NEAR[1],
        phones=("0321 6966621",),
    )
    report = corroborate_run(db_session, run, source=StubSource(_harvest(listing)))

    assert report.matched == 1
    assert report.contacts_added == 1
    db_session.refresh(business)
    numbers = {c.value_e164: c for c in business.contacts}
    assert set(numbers) == {"+924237114000", "+923216966621"}
    added = numbers["+923216966621"]
    assert added.source == Source.BUSINESSLIST_PK
    assert added.source_url.startswith("https://www.businesslist.pk/company/")
    assert float(added.confidence) == DIRECTORY_CONTACT_CONFIDENCE


@requires_db
def test_a_directory_never_lowers_a_confirmed_whatsapp_label(db_session: Session) -> None:
    """Evidence only ever moves up (§5.2). A directory republishes a bare number,
    which §9.3 scores at 0.60 — it must not overwrite the 1.00 a business's own
    ``wa.me`` link proved."""
    run = _run(db_session)
    business = _saved(db_session, run, "Pizza M21", lat=GULBERG[0], lng=GULBERG[1])
    contact = Contact(
        business_id=business.id,
        kind=ContactKind.PHONE,
        value_raw="0321 6966621",
        value_e164="+923216966621",
        line_type=LineType.MOBILE,
        wa_evidence=1.00,
        wa_label=WhatsAppLabel.CONFIRMED,
        wa_evidence_url="https://pizzam21.pk/contact",
        source=Source.BUSINESS_WEBSITE,
        source_url="https://pizzam21.pk/",
        confidence=0.95,
        scraped_at=datetime.now(UTC),
    )
    db_session.add(contact)
    db_session.flush()

    listing = _listing(
        "Pizza M21", lat=GULBERG_NEAR[0], lng=GULBERG_NEAR[1], phones=("0321 6966621",)
    )
    corroborate_run(db_session, run, source=StubSource(_harvest(listing)))

    db_session.refresh(contact)
    assert float(contact.wa_evidence) == 1.00
    assert contact.wa_label == WhatsAppLabel.CONFIRMED
    # Provenance is not rewritten: the number still came from the website.
    assert contact.source == Source.BUSINESS_WEBSITE
    assert contact.wa_evidence_url == "https://pizzam21.pk/contact"


@requires_db
def test_a_match_raises_source_agreement_which_is_the_scoring_payoff(
    db_session: Session,
) -> None:
    """§10.2 gives 10 points to ``n_sources >= 2``. Until Phase 6 only Maps and
    the business's own website could ever agree, so a business with no website
    could never earn the term at all."""
    run = _run(db_session)
    business = _saved(
        db_session, run, "Pizza M21", lat=GULBERG[0], lng=GULBERG[1], phones=("04237114000",)
    )
    assert {c.source for c in business.contacts} == {Source.GOOGLE_MAPS}

    listing = _listing(
        "Pizza M21", lat=GULBERG_NEAR[0], lng=GULBERG_NEAR[1], phones=("0321 6966621",)
    )
    corroborate_run(db_session, run, source=StubSource(_harvest(listing)))

    db_session.refresh(business)
    assert {c.source for c in business.contacts} == {
        Source.GOOGLE_MAPS,
        Source.BUSINESSLIST_PK,
    }


@requires_db
def test_gap_fill_never_overwrites_a_field_maps_already_published(
    db_session: Session,
) -> None:
    """§10.1's fill-only merge rule. Two sources' review counts are not the same
    quantity, and the business's own platform reports the better one."""
    run = _run(db_session)
    business = _saved(
        db_session,
        run,
        "Bombay Biryani",
        lat=GULBERG[0],
        lng=GULBERG[1],
        address="43 Lower Mall, Lahore",
        rating=4.2,
        phones=("04237114000",),
    )
    listing = _listing(
        "Bombay Biryani",
        lat=GULBERG_NEAR[0],
        lng=GULBERG_NEAR[1],
        phones=("04237114000",),
        address="SOMEWHERE ELSE",
        rating=5.0,
        review_count=1,
    )
    report = corroborate_run(db_session, run, source=StubSource(_harvest(listing)))

    db_session.refresh(business)
    assert business.address == "43 Lower Mall, Lahore"
    assert float(business.rating) == 4.2
    # review_count was genuinely missing, so the directory fills it — this is the
    # hole §10.2 called the most discriminating input to `business_signal`.
    assert business.review_count == 1
    assert report.fields_gap_filled == 1


@requires_db
def test_an_unmatched_row_is_not_inserted_by_default(db_session: Session) -> None:
    """§5.3: not a volume driver. An unmatched row is at least as likely to be a
    business the join missed as one Maps never saw, and inserting it then
    manufactures the duplicate §10.1 exists to prevent."""
    run = _run(db_session)
    _saved(db_session, run, "Pizza M21", lat=GULBERG[0], lng=GULBERG[1])
    listing = _listing("Totally Different Cafe", lat=DHA[0], lng=DHA[1], phones=("03001234567",))

    report = corroborate_run(
        db_session, run, source=StubSource(_harvest(listing)), insert_unmatched=False
    )

    assert report.unmatched == 1
    assert report.unmatched_inserted == 0
    assert report.refusals == {Refusal.REJECTED_DISTANCE.value: 1}
    assert db_session.query(Business).filter(Business.run_id == run.id).count() == 1


@requires_db
def test_unmatched_rows_can_be_inserted_when_the_operator_asks(db_session: Session) -> None:
    """§5.3's other reading — a thin discovery source where Maps is weak — is
    real for narrow categories, so the behaviour exists behind a flag."""
    run = _run(db_session)
    _saved(db_session, run, "Pizza M21", lat=GULBERG[0], lng=GULBERG[1])
    listing = _listing("Totally Different Cafe", lat=DHA[0], lng=DHA[1], phones=("03001234567",))

    report = corroborate_run(
        db_session, run, source=StubSource(_harvest(listing)), insert_unmatched=True
    )

    assert report.unmatched_inserted == 1
    inserted = (
        db_session.query(Business)
        .filter(Business.run_id == run.id, Business.name == "Totally Different Cafe")
        .one()
    )
    assert inserted.place_id is None
    assert [c.source for c in inserted.contacts] == [Source.BUSINESSLIST_PK]


# --------------------------------------------------------------------------- #
# §5.5 — a source that could not do its work must not report success
# --------------------------------------------------------------------------- #


@requires_db
def test_a_thin_category_is_not_a_failure(db_session: Session) -> None:
    """§5.3 measured 18 beauty salons in the whole of Lahore. A small answer is
    this directory telling the truth about its size, and marking the run partial
    for it would cry wolf on every salon run."""
    run = _run(db_session)
    _saved(db_session, run, "Pizza M21", lat=GULBERG[0], lng=GULBERG[1])
    report = corroborate_run(db_session, run, source=StubSource(_harvest()))

    assert report.listings_found == 0
    assert run.status is RunStatus.DONE
    assert run.error is None


@requires_db
def test_no_category_answering_is_the_5_5_signature_and_marks_the_run_partial(
    db_session: Session,
) -> None:
    """Asking for three categories and having none answer is a different fact
    from a category that answered with nothing in it."""
    run = _run(db_session)
    _saved(db_session, run, "Pizza M21", lat=GULBERG[0], lng=GULBERG[1])
    report = corroborate_run(
        db_session,
        run,
        source=StubSource(_harvest(categories_requested=3, categories_answered=0)),
    )

    assert run.status is RunStatus.PARTIAL
    assert "none of the 3 BusinessList categories answered" in (run.error or "")
    assert report.listings_found == 0


@requires_db
def test_listings_without_a_single_phone_trips_the_extractor_check(
    db_session: Session,
) -> None:
    """Phone fill measured at 84%. Zero from a non-empty harvest means the phone
    cell moved, not that the directory stopped publishing numbers."""
    run = _run(db_session)
    _saved(db_session, run, "Pizza M21", lat=GULBERG[0], lng=GULBERG[1])
    listings = [_listing(f"Place {i}", directory_id=str(i)) for i in range(5)]
    corroborate_run(db_session, run, source=StubSource(_harvest(*listings)))

    assert run.status is RunStatus.PARTIAL
    assert "not one phone number" in (run.error or "")


@requires_db
def test_a_refused_source_marks_the_run_partial_rather_than_done(
    db_session: Session,
) -> None:
    """§5.5's convention at the stage boundary: a run that could not do its work
    reports `partial`, and `partial` and `done` are different facts."""
    run = _run(db_session)
    _saved(db_session, run, "Pizza M21", lat=GULBERG[0], lng=GULBERG[1])
    corroborate_run(
        db_session,
        run,
        source=StubSource(_harvest(refused=True, error="http_429", categories_answered=0)),
    )

    assert run.status is RunStatus.PARTIAL
    assert "refused" in (run.error or "")


@requires_db
def test_the_report_records_5_3s_coverage_so_it_cannot_be_forgotten(
    db_session: Session,
) -> None:
    """The directory's own "We found N listings" total, per slice, per run —
    §5.3's coverage warning as a measurement rather than a quotation."""
    run = _run(db_session)
    _saved(
        db_session, run, "Pizza M21", lat=GULBERG[0], lng=GULBERG[1], phones=("04237114000",)
    )
    harvest = _harvest(
        _listing("Pizza M21", lat=GULBERG[0], lng=GULBERG[1], phones=("04237114000",))
    )
    harvest.totals = {"restaurants": 59}
    report = corroborate_run(db_session, run, source=StubSource(harvest))

    # ``corroborate_run`` returns the report and ``run_directory_corroboration``
    # persists it into ``runs.stats``, the same split ``enrich_run`` uses — so
    # the counter and the stage wrapper cannot drift into two shapes.
    assert report.directory_totals == {"restaurants": 59}
    assert report.as_dict()["directory_totals"] == {"restaurants": 59}

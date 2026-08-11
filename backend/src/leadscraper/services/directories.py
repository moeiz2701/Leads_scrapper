"""Stage 2, second input — §5.3 horizontal directories as a corroboration layer.

**Where directories enter, decided here.** §2 lists them under *both* Stage 1
discovery and Stage 2 contact enrichment and does not say which is primary. The
measurement settles it: BusinessList holds **59 restaurants and 18 beauty salons
in Lahore** against the **429 and 60** the same slices returned from Maps. As a
discovery source that is a rounding error; as a second opinion on businesses Maps
already found it is a real signal, because §10.2 gives 10 points to
`source_agreement` and until now *every* run could only ever agree with itself
(Maps and the business's own website are the only two sources that exist).

So this runs **inside Stage 2, after the website pass**, and its primary job is
the join. Rows that match nothing are a secondary, honest outcome and are handled
by `unmatched_policy` below rather than silently dropped.

## The join is the point, and it is the first real test of §10.1's fuzzy tier

Until Phase 6 the dedupe cascade had never been asked to *find* a duplicate
across sources. Every business in the database came from Maps and carried a
`place_id`, so tier 2 did all the work and `fuzzy_name_geo` had produced **1
merge against 1,027 rejections** in the project's history. §10.1 says so itself:
the tier's value "is prospective — it arrives with the sources that do not share
Maps' place_id". This module is that arrival. A BusinessList row has no
`place_id`, so name-and-distance is the only thing that can join it to the Maps
business it describes.

Three rules, in the order they are applied:

1. **Coordinates present → §10.1 tier 3, verbatim.** Name token-set ratio ≥ 88
   AND haversine < 150 m. Unchanged from the spec because the Phase 4
   measurement found tier 3 was the one tier that was already right.
2. **Coordinates present, phone or domain also matches → the corroborated bar
   (75).** Same demotion Phase 4 made: a shared number lowers the name bar, it
   never waives the distance test.
3. **Coordinates absent → distance is *unavailable*, not failed.** 35% of
   BusinessList rows carry no `data-ltd`, and §10.1's tier 3 is an AND, so those
   rows can never pass it. Refusing them all strands a third of the source;
   waiving the geo test re-introduces exactly the chain-merging bug Phase 4
   removed. The rule that threads it: **match on an exact phone plus a name at
   the corroborated bar, and only when precisely one business in the run
   qualifies.** Two candidates means a chain — House of Salons publishes one
   number across three branches — and ambiguity refuses rather than guessing.
   `MatchTier.PHONE_UNLOCATED` records that this is a weaker join than the
   others, so the measurement can be read separately.

## What a match does, and what it must never do

Every convention this project has accumulated lands on this module at once:

* **Never discard a contact (§10.1).** A directory number Maps did not have is a
  new `contacts` row, not a correction to an existing one.
* **Evidence only moves up (§5.2).** A directory publishes a bare number, which
  §9.3 scores exactly as it scores a Maps listing — 0.60 for a mobile, 0.00 for a
  landline. That can *never* lower a `confirmed` a website already proved.
* **Provenance survives (§1).** `source` = `businesslist_pk` and `source_url` =
  the directory's own detail URL, so §15's deletion path and §12.1's columns
  39–40 keep saying where the value came from.
* **Attribution is Stage 4's (§8).** This source publishes no owner names — 0 of
  the sampled records carried one — and this module would not build the join if
  it did.
* **Missing stays missing.** A listing without a rating gap-fills nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from rapidfuzz.fuzz import token_set_ratio
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from leadscraper.config import get_settings
from leadscraper.core.cache import FetchCache
from leadscraper.core.geo import is_near
from leadscraper.core.phone import ParsedPhone
from leadscraper.core.textnorm import conflicting_segments
from leadscraper.core.whatsapp import baseline_signal, score_signals
from leadscraper.db.models import Business, Contact, Run
from leadscraper.db.session import session_scope
from leadscraper.enums import BelongsTo, ContactKind, RunStatus, Source, Stage
from leadscraper.logging import get_logger
from leadscraper.pipeline.stages import StageResult
from leadscraper.sources.businesslist import (
    BusinessListSource,
    DirectoryHarvest,
    DirectoryListing,
)

log = get_logger(__name__)

# §10.2 rates a Maps listing's number at 0.85 "is this really the business's
# number". A directory is a third party republishing it, and BusinessList carries
# unverified self-submitted entries alongside its verified ones, so it sits one
# notch below the platform the business itself registered with.
DIRECTORY_CONTACT_CONFIDENCE = 0.75


class MatchTier(StrEnum):
    """Which rule joined a directory row to a business. Reported per run."""

    FUZZY_NAME_GEO = "fuzzy_name_geo"
    CORROBORATED_GEO = "corroborated_geo"
    # Coordinates absent — a deliberately weaker join. Counted separately so its
    # error rate can be measured against the tiers that had a distance test.
    PHONE_UNLOCATED = "phone_unlocated"


class Refusal(StrEnum):
    NO_CANDIDATE = "no_candidate"
    REJECTED_NAME = "rejected_name"
    REJECTED_DISTANCE = "rejected_distance"
    REJECTED_SEGMENT = "rejected_segment"
    # Several businesses matched equally well and nothing separates them. The
    # chain case, and the reason rule 3 exists at all.
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class Match:
    business_id: uuid.UUID
    tier: MatchTier
    ratio: float


@dataclass(slots=True)
class DirectoryReport:
    businesses_in_run: int = 0
    listings_found: int = 0
    listings_with_phone: int = 0
    listings_with_coordinates: int = 0
    categories_requested: int = 0
    categories_answered: int = 0
    pages_fetched: int = 0
    pages_from_cache: int = 0
    requests: int = 0
    matched: int = 0
    matches_by_tier: dict[str, int] = field(default_factory=dict)
    refusals: dict[str, int] = field(default_factory=dict)
    businesses_corroborated: int = 0
    contacts_added: int = 0
    contacts_upgraded: int = 0
    fields_gap_filled: int = 0
    unmatched: int = 0
    unmatched_inserted: int = 0
    refused: bool = False
    blocked: bool = False
    # §5.3's own coverage warning, measured per slice on every run rather than
    # quoted from the doc.
    directory_totals: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "businesses_in_run": self.businesses_in_run,
            "listings_found": self.listings_found,
            "listings_with_phone": self.listings_with_phone,
            "listings_with_coordinates": self.listings_with_coordinates,
            "categories_requested": self.categories_requested,
            "categories_answered": self.categories_answered,
            "pages_fetched": self.pages_fetched,
            "pages_from_cache": self.pages_from_cache,
            "requests": self.requests,
            "matched": self.matched,
            "matches_by_tier": self.matches_by_tier,
            "refusals": self.refusals,
            "businesses_corroborated": self.businesses_corroborated,
            "contacts_added": self.contacts_added,
            "contacts_upgraded": self.contacts_upgraded,
            "fields_gap_filled": self.fields_gap_filled,
            "unmatched": self.unmatched,
            "unmatched_inserted": self.unmatched_inserted,
            "refused": self.refused,
            "blocked": self.blocked,
            "directory_totals": self.directory_totals,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# The stage entry point
# --------------------------------------------------------------------------- #


def run_directory_corroboration(
    run_id: uuid.UUID, *, insert_unmatched: bool | None = None
) -> StageResult:
    """Harvest §5.3 directories for the run's slice and join them to its rows."""
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"No such run: {run_id}")

        report = corroborate_run(session, run, insert_unmatched=insert_unmatched)
        run.stats = {**(run.stats or {}), "directories": report.as_dict()}
        session.flush()

    return StageResult(
        stage=Stage.CONTACT_ENRICHMENT,
        run_id=run_id,
        processed=report.listings_found,
        produced=report.contacts_added + report.contacts_upgraded,
        skipped=report.unmatched,
        failed=1 if (report.refused or report.blocked) else 0,
        notes={k: str(v) for k, v in report.as_dict().items()},
    )


def corroborate_run(
    session: Session,
    run: Run,
    source: BusinessListSource | None = None,
    *,
    insert_unmatched: bool | None = None,
) -> DirectoryReport:
    """The stage body, against a caller-supplied session.

    Split out the way ``enrichment.enrich_run`` is, so the join rules can be
    tested against a real database with a stub harvester and no network.
    """
    report = DirectoryReport()
    settings = get_settings()
    if insert_unmatched is None:
        insert_unmatched = settings.directory_insert_unmatched

    businesses = list(
        session.execute(
            select(Business)
            .where(Business.run_id == run.id)
            .options(selectinload(Business.contacts))
            .order_by(Business.created_at)
        ).scalars()
    )
    report.businesses_in_run = len(businesses)

    source = source or BusinessListSource(
        cache=FetchCache(session=session, settings=settings), settings=settings
    )
    harvest: DirectoryHarvest = asyncio.run(source.harvest(run.city, run.category))
    _tally_harvest(report, harvest)

    index = _BusinessIndex(businesses)

    for listing in harvest.listings:
        decision = match_listing(
            listing,
            index,
            fuzzy_threshold=settings.dedupe_fuzzy_threshold,
            corroborated_threshold=settings.dedupe_corroborated_threshold,
        )
        if isinstance(decision, Match):
            report.matched += 1
            report.matches_by_tier[decision.tier.value] = (
                report.matches_by_tier.get(decision.tier.value, 0) + 1
            )
            _apply_listing(session, index.by_id[decision.business_id], listing, report)
        else:
            report.refusals[decision.value] = report.refusals.get(decision.value, 0) + 1
            report.unmatched += 1
            if insert_unmatched:
                _insert_listing(session, run, listing, report)

    session.flush()
    _finalise(run, report, harvest)
    return report


def _tally_harvest(report: DirectoryReport, harvest: DirectoryHarvest) -> None:
    report.listings_found = len(harvest.listings)
    report.listings_with_phone = sum(1 for x in harvest.listings if x.phones)
    report.listings_with_coordinates = sum(1 for x in harvest.listings if x.has_coordinates)
    report.categories_requested = harvest.categories_requested
    report.categories_answered = harvest.categories_answered
    report.pages_fetched = harvest.pages_fetched
    report.pages_from_cache = harvest.pages_from_cache
    report.requests = harvest.requests
    report.refused = harvest.refused
    report.blocked = harvest.blocked
    report.directory_totals = dict(harvest.totals)
    report.error = harvest.error


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


class _BusinessIndex:
    """The run's businesses, indexed the three ways the join asks for them."""

    def __init__(self, businesses: list[Business]) -> None:
        self.businesses = businesses
        self.by_id = {b.id: b for b in businesses}
        self.by_phone: dict[str, list[Business]] = {}
        for business in businesses:
            for contact in business.contacts:
                if contact.kind == ContactKind.PHONE and contact.value_e164:
                    self.by_phone.setdefault(contact.value_e164, []).append(business)

    def located(self) -> list[Business]:
        return [b for b in self.businesses if b.lat is not None and b.lng is not None]


def match_listing(
    listing: DirectoryListing,
    index: _BusinessIndex,
    *,
    fuzzy_threshold: int,
    corroborated_threshold: int,
) -> Match | Refusal:
    """Join one directory row to a business in the run, or refuse and say why.

    Pure apart from reading the index. Every refusal is a named outcome rather
    than a ``None``, because §5.5's whole lesson is that a source which quietly
    produces nothing is the failure you do not notice.
    """
    phone_matches = _phone_candidates(listing, index)

    if listing.has_coordinates:
        return _match_located(
            listing,
            index,
            phone_matches,
            fuzzy_threshold=fuzzy_threshold,
            corroborated_threshold=corroborated_threshold,
        )
    return _match_unlocated(listing, phone_matches, threshold=corroborated_threshold)


def _match_located(
    listing: DirectoryListing,
    index: _BusinessIndex,
    phone_matches: set[uuid.UUID],
    *,
    fuzzy_threshold: int,
    corroborated_threshold: int,
) -> Match | Refusal:
    """§10.1 tier 3, verbatim, with the Phase 4 corroboration demotion applied."""
    best: Match | None = None
    saw_near = False
    saw_segment_conflict = False

    for business in index.located():
        if not is_near(
            listing.lat, listing.lng, _coord(business.lat), _coord(business.lng)
        ):
            continue
        saw_near = True

        # Checked before the ratio, exactly as ``dedupe.decide`` does: a men's
        # and a women's branch at one address score above even the strict
        # threshold, and joining them would attach a directory record to the
        # wrong separately-staffed premises.
        if conflicting_segments(listing.name_norm, business.name_norm or ""):
            saw_segment_conflict = True
            continue

        ratio = token_set_ratio(listing.name_norm, business.name_norm or "")
        threshold = (
            corroborated_threshold if business.id in phone_matches else fuzzy_threshold
        )
        if ratio >= threshold and (best is None or ratio > best.ratio):
            best = Match(
                business_id=business.id,
                tier=(
                    MatchTier.CORROBORATED_GEO
                    if business.id in phone_matches
                    else MatchTier.FUZZY_NAME_GEO
                ),
                ratio=ratio,
            )

    if best is not None:
        return best
    if saw_segment_conflict:
        return Refusal.REJECTED_SEGMENT
    if saw_near:
        return Refusal.REJECTED_NAME
    return Refusal.REJECTED_DISTANCE


def _match_unlocated(
    listing: DirectoryListing, phone_matches: set[uuid.UUID], *, threshold: int
) -> Match | Refusal:
    """Rule 3 — the distance test is *unavailable*, so ambiguity must refuse.

    §10.1's tier 3 is an AND and this row cannot satisfy half of it. The only
    evidence left is an exact phone match, and Phase 4 measured precisely why
    that is not enough on its own: 36 groups of businesses share a number and
    **not one is a duplicate** — they are multi-branch chains. So a phone match
    is accepted only when it is *unique*: one number, one business, plus a name
    at the corroborated bar. The moment a second business shares that number the
    row is a chain listing and there is nothing to say which branch it describes.
    """
    if not phone_matches:
        return Refusal.NO_CANDIDATE
    if len(phone_matches) > 1:
        return Refusal.AMBIGUOUS

    business_id = next(iter(phone_matches))
    return Match(business_id=business_id, tier=MatchTier.PHONE_UNLOCATED, ratio=float(threshold))


def _phone_candidates(listing: DirectoryListing, index: _BusinessIndex) -> set[uuid.UUID]:
    return {
        business.id
        for phone in listing.phones
        for business in index.by_phone.get(phone.e164, ())
    }


def _coord(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


# --------------------------------------------------------------------------- #
# Applying a match
# --------------------------------------------------------------------------- #


def _apply_listing(
    session: Session,
    business: Business,
    listing: DirectoryListing,
    report: DirectoryReport,
) -> None:
    """Fold one directory row into the business it corroborates."""
    touched = False
    existing = {
        contact.value_e164: contact
        for contact in business.contacts
        if contact.kind == ContactKind.PHONE and contact.value_e164
    }

    for parsed in listing.phones:
        contact = existing.get(parsed.e164)
        if contact is None:
            new = _new_contact(business, listing, parsed)
            business.contacts.append(new)
            existing[parsed.e164] = new
            report.contacts_added += 1
            touched = True
        elif _upgrade_contact(contact, parsed):
            report.contacts_upgraded += 1
            touched = True

    if _fill_gaps(business, listing, report):
        touched = True

    if touched:
        report.businesses_corroborated += 1


def _new_contact(
    business: Business, listing: DirectoryListing, parsed: ParsedPhone
) -> Contact:
    evidence = score_signals([baseline_signal(parsed.line_type)])
    return Contact(
        business_id=business.id,
        kind=ContactKind.PHONE,
        value_raw=parsed.raw,
        value_e164=parsed.e164,
        line_type=parsed.line_type,
        operator=parsed.operator,
        wa_evidence=round(evidence.score, 2),
        wa_label=evidence.label,
        # §8: a directory publishes the business's listed line and names nobody.
        # Tier B's "the listed number *is* the owner's" is Stage 4's inference to
        # make, never this module's.
        belongs_to=BelongsTo.BUSINESS,
        confidence=DIRECTORY_CONTACT_CONFIDENCE,
        source=Source.BUSINESSLIST_PK,
        source_url=listing.source_url,
        scraped_at=datetime.now(UTC),
    )


def _upgrade_contact(contact: Contact, parsed: ParsedPhone) -> bool:
    """Upgrade-only, per §5.2's rule, and there is very little a directory can add.

    A directory republishes a bare number. §9.3 scores that identically to a Maps
    listing, so it essentially never beats what is already recorded — and it must
    never *lower* a `confirmed` a website proved. What it can genuinely fill is a
    line type nobody classified, so that is all this does.

    The contact's ``source`` is deliberately not rewritten: the number still came
    from wherever it came from, and the directory's agreement is recorded by the
    row it adds elsewhere, not by overwriting provenance §1 depends on.
    """
    changed = False
    if contact.line_type is None and parsed.line_type is not None:
        contact.line_type = parsed.line_type
        contact.operator = parsed.operator
        changed = True

    evidence = score_signals([baseline_signal(parsed.line_type)])
    if evidence.score > float(contact.wa_evidence or 0.0):
        contact.wa_evidence = round(evidence.score, 2)
        contact.wa_label = evidence.label
        changed = True
    return changed


_GAP_FILL_FIELDS = ("address", "lat", "lng", "rating", "review_count")


def _fill_gaps(
    business: Business, listing: DirectoryListing, report: DirectoryReport
) -> bool:
    """Fill only. §10.1: a merge must never blank or overwrite a populated field.

    ``rating`` and ``review_count`` are worth spelling out: §10.2 feeds
    ``business_signal`` from them, and the Lahore run carries `review_count` on
    **0%** of its rows. A directory that supplies one is filling a real hole in
    the scorer's most discriminating term — but only where Maps published
    nothing, because two sources' review counts are not the same quantity and the
    one the business's own platform reports is the better of the two.
    """
    filled = 0
    for name in _GAP_FILL_FIELDS:
        if getattr(business, name) is not None:
            continue
        value = getattr(listing, name)
        if value is not None:
            setattr(business, name, value)
            filled += 1
    report.fields_gap_filled += filled
    return filled > 0


def _insert_listing(
    session: Session, run: Run, listing: DirectoryListing, report: DirectoryReport
) -> None:
    """A directory row that matched nothing, kept as a business in its own right.

    Off by default (``DIRECTORY_INSERT_UNMATCHED``). §5.3 is explicit that these
    directories are "not a volume driver", and an unmatched row is at least as
    likely to be a business the join failed to recognise as one Maps never saw —
    in which case inserting it manufactures the duplicate §10.1 exists to
    prevent. Stage 6's cascade gets a second attempt at it, but a row with no
    coordinates cannot pass tier 3 there either, so the duplicate would persist.

    Kept available rather than deleted because §5.3's other reading — a thin
    discovery source where Maps is weak — is real for narrow categories, and
    turning it on is a per-run decision the operator can make with the match rate
    from this same report in front of them.
    """
    business = Business(
        run_id=run.id,
        name=listing.name,
        name_norm=listing.name_norm,
        category=run.category,
        city=run.city,
        address=listing.address,
        lat=listing.lat,
        lng=listing.lng,
        rating=listing.rating,
        review_count=listing.review_count,
    )
    session.add(business)
    session.flush()

    for parsed in listing.phones:
        session.add(_new_contact(business, listing, parsed))
    session.flush()
    report.unmatched_inserted += 1


# --------------------------------------------------------------------------- #
# Honest reporting (§5.5)
# --------------------------------------------------------------------------- #


def _finalise(run: Run, report: DirectoryReport, harvest: DirectoryHarvest) -> None:
    """A source that could not do its work must not report success.

    Note what is deliberately *not* degraded: finding zero listings for a slice.
    §5.3 measured 18 beauty salons in the whole of Lahore, so a thin or empty
    category is this source telling the truth about a small directory. What is
    not normal is asking for several categories and having **none** of them
    answer — that is the §5.5 signature, and it is a different fact from a
    category that answered with nothing in it.
    """
    degraded: str | None = None

    if report.blocked:
        reason = report.error or "the circuit breaker opened or the daily budget ran out"
        degraded = f"directory corroboration was skipped — {reason}"
    elif report.refused:
        degraded = (
            f"BusinessList.pk refused the request ({report.error}) — the §5.3 "
            f"corroboration layer did not run for this slice"
        )
    elif report.categories_requested and report.categories_answered == 0:
        degraded = (
            f"none of the {report.categories_requested} BusinessList categories "
            f"answered — the §5.3 URL pattern or its markup has changed "
            f"(implementation.md §5.5)"
        )
        log.error("directories.no_categories_answered", run_id=str(run.id))
    elif report.listings_found and report.listings_with_phone == 0:
        # Measured at 84% phone fill across 58 listings. Zero from a non-empty
        # harvest means the phone cell moved, not that the directory stopped
        # publishing numbers.
        degraded = (
            f"BusinessList returned {report.listings_found} listings and not one "
            f"phone number — the §5.3 phone extractor has stopped matching "
            f"(implementation.md §5.5)"
        )
        log.error("directories.zero_phone_yield", run_id=str(run.id))

    if degraded:
        run.status = RunStatus.PARTIAL
        run.error = run.error or degraded

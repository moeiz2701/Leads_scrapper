"""Stage 6 — the §10.1 dedupe cascade and merge. (CSV export is Phase 5.)

Scope: **within one run.** §10.1 does not say, so this is a decision, recorded
here and in the §10.1 note:

* A ``businesses`` row belongs to exactly one run (``run_id`` is ``NOT NULL``),
  so a cross-run merge has nowhere to put the survivor. Whichever run owned it
  would be claiming a business the other one found, which destroys the record
  §16's "validate by re-running" depends on.
* ``place_id`` is unique *per run* by design (README departure #1) precisely so a
  run can be repeated. Dedupe operating across runs would undo that.
* Measured on the four Lahore × salon runs sitting in the database: 232 place_ids
  collapse to 72, and three of the four runs overlap 100% with each other. A
  cross-run merge would not be deduplicating a table — it would be deleting
  three runs.

The operator's real want — one table, not four — is a **read-side** concern and
belongs to Phase 5: the results view can union runs and collapse on ``place_id``
at query time without destroying anything. Flagged rather than built here.

## What the cascade actually does

§10.1 lists four tiers. Run against the two enriched runs, two of them turned out
to be destructive as written — see the §10.1 note in implementation.md for the
full measurement. The correction, in one line: **``place_id`` merges on its own;
every other tier must also pass the 150 m distance test.**

Phone and domain become *corroborating* evidence that lowers the name-similarity
bar, rather than merge keys in their own right. §10.1's own tier 3 already pairs
name similarity with distance, and the measurement says distance is the term
carrying the discrimination — every shared-number and shared-domain group in both
runs is a multi-branch chain or two unrelated shops in one plaza, and not one is
a duplicate.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import cast

from rapidfuzz.fuzz import token_set_ratio
from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session, selectinload

from leadscraper.config import get_settings
from leadscraper.core.geo import NEAR_DUPLICATE_METRES, grid_cell, is_near, neighbouring_cells
from leadscraper.core.textnorm import conflicting_segments, registrable_domain
from leadscraper.db.models import Business, Contact, Run
from leadscraper.db.session import session_scope
from leadscraper.enums import ContactKind, Stage
from leadscraper.logging import get_logger
from leadscraper.pipeline.stages import StageResult
from leadscraper.services.scoring import score_run

log = get_logger(__name__)


class MatchTier(StrEnum):
    """§10.1's cascade, in the order it is applied."""

    EXACT_PHONE = "exact_phone"
    PLACE_ID = "place_id"
    FUZZY_NAME_GEO = "fuzzy_name_geo"
    DOMAIN = "domain"


class Verdict(StrEnum):
    MERGE = "merge"
    # Same number or same domain, but too far apart to be the same premises —
    # i.e. a chain. The single most common outcome in real data.
    REJECTED_DISTANCE = "rejected_distance"
    # Same premises, but the names describe two different businesses. Two salons
    # in one plaza sharing a landline is the shape this catches.
    REJECTED_NAME = "rejected_name"
    # Same premises, same brand, different clientele — a men's and a women's
    # branch at one address. Name similarity cannot see this; see
    # ``textnorm.conflicting_segments``.
    REJECTED_SEGMENT = "rejected_segment"


@dataclass(frozen=True, slots=True)
class Candidate:
    left: uuid.UUID
    right: uuid.UUID
    tier: MatchTier


@dataclass(slots=True)
class DedupeReport:
    businesses_before: int = 0
    businesses_after: int = 0
    candidates: dict[str, int] = field(default_factory=dict)
    merges_by_tier: dict[str, int] = field(default_factory=dict)
    rejected_distance: int = 0
    rejected_name: int = 0
    rejected_segment: int = 0
    groups_merged: int = 0
    businesses_absorbed: int = 0
    contacts_moved: int = 0
    # Post-merge, the same number can sit on two rows of one business — each a
    # real provenance record. §3.3 ranking gives the slot to one of them.
    duplicate_numbers_after_merge: int = 0
    rescored: int = 0

    def as_dict(self) -> dict:
        return {
            "businesses_before": self.businesses_before,
            "businesses_after": self.businesses_after,
            "candidates": self.candidates,
            "merges_by_tier": self.merges_by_tier,
            "rejected_distance": self.rejected_distance,
            "rejected_name": self.rejected_name,
            "rejected_segment": self.rejected_segment,
            "groups_merged": self.groups_merged,
            "businesses_absorbed": self.businesses_absorbed,
            "contacts_moved": self.contacts_moved,
            "duplicate_numbers_after_merge": self.duplicate_numbers_after_merge,
            "rescored": self.rescored,
        }


def run_dedupe(run_id: uuid.UUID) -> StageResult:
    """Collapse duplicate businesses in a run, then re-score the survivors."""
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"No such run: {run_id}")

        report = dedupe_run(session, run)
        run.stats = {**(run.stats or {}), "dedupe": report.as_dict()}
        session.flush()

    return StageResult(
        stage=Stage.DEDUPE_EXPORT,
        run_id=run_id,
        processed=report.businesses_before,
        produced=report.businesses_after,
        skipped=report.businesses_absorbed,
        notes={k: str(v) for k, v in report.as_dict().items()},
    )


def dedupe_run(session: Session, run: Run) -> DedupeReport:
    """The stage body, against a caller-supplied session."""
    report = DedupeReport()
    settings = get_settings()

    businesses = list(
        session.execute(
            select(Business)
            .where(Business.run_id == run.id)
            .options(selectinload(Business.contacts))
            .order_by(Business.created_at)
        ).scalars()
    )
    report.businesses_before = len(businesses)
    if len(businesses) < 2:
        report.businesses_after = len(businesses)
        return report

    by_id = {b.id: b for b in businesses}
    candidates = _candidates(businesses, report)

    groups = _Union()
    merges: defaultdict[str, int] = defaultdict(int)
    for candidate in candidates:
        verdict = decide(
            by_id[candidate.left],
            by_id[candidate.right],
            candidate.tier,
            fuzzy_threshold=settings.dedupe_fuzzy_threshold,
            corroborated_threshold=settings.dedupe_corroborated_threshold,
        )
        if verdict is Verdict.MERGE:
            if groups.union(candidate.left, candidate.right):
                merges[candidate.tier.value] += 1
        elif verdict is Verdict.REJECTED_DISTANCE:
            report.rejected_distance += 1
        elif verdict is Verdict.REJECTED_SEGMENT:
            report.rejected_segment += 1
        else:
            report.rejected_name += 1

    report.merges_by_tier = dict(merges)

    survivors = [
        _merge_group(session, [by_id[i] for i in members], report)
        for members in groups.groups()
    ]

    session.flush()
    report.businesses_after = report.businesses_before - report.businesses_absorbed

    _rescore_survivors(session, run, survivors, report)
    _finalise(report)
    return report


# --------------------------------------------------------------------------- #
# Candidate generation — the four §10.1 tiers
# --------------------------------------------------------------------------- #


def _candidates(businesses: list[Business], report: DedupeReport) -> list[Candidate]:
    """Every pair worth comparing, tagged with the tier that proposed it.

    Tiers are generated cheapest-first and a pair proposed by several tiers keeps
    the strongest, so ``decide`` sees each pair once.
    """
    proposals: dict[tuple[uuid.UUID, uuid.UUID], MatchTier] = {}
    counts: defaultdict[str, int] = defaultdict(int)

    def propose(a: uuid.UUID, b: uuid.UUID, tier: MatchTier) -> None:
        key = (a, b) if a < b else (b, a)
        counts[tier.value] += 1
        current = proposals.get(key)
        if current is None or _TIER_RANK[tier] < _TIER_RANK[current]:
            proposals[key] = tier

    for tier, keys in (
        (MatchTier.EXACT_PHONE, _phone_keys),
        (MatchTier.PLACE_ID, _place_id_keys),
        (MatchTier.DOMAIN, _domain_keys),
    ):
        buckets: defaultdict[str, list[uuid.UUID]] = defaultdict(list)
        for business in businesses:
            for key in keys(business):
                buckets[key].append(business.id)
        for members in buckets.values():
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    propose(members[i], members[j], tier)

    for a, b in _near_pairs(businesses):
        propose(a, b, MatchTier.FUZZY_NAME_GEO)

    report.candidates = dict(counts)
    return [Candidate(a, b, tier) for (a, b), tier in proposals.items()]


# Which tier's label a pair keeps when several propose it. ``place_id`` first
# because it is the only one that merges unconditionally.
_TIER_RANK = {
    MatchTier.PLACE_ID: 0,
    MatchTier.EXACT_PHONE: 1,
    MatchTier.DOMAIN: 2,
    MatchTier.FUZZY_NAME_GEO: 3,
}


def _phone_keys(business: Business) -> list[str]:
    return [
        c.value_e164
        for c in business.contacts
        if c.kind == ContactKind.PHONE and c.value_e164
    ]


def _place_id_keys(business: Business) -> list[str]:
    return [business.place_id] if business.place_id else []


def _domain_keys(business: Business) -> list[str]:
    domain = registrable_domain(business.website) if business.website else None
    return [domain] if domain else []


# A business reduced to what the blocking grid needs: id and plain-float coords.
_Point = tuple[uuid.UUID, float, float]


def _near_pairs(businesses: list[Business]) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """§10.1 tier 3's candidate set, blocked on a geographic grid.

    A full pairwise scan is 20k comparisons on the 199-business Islamabad run and
    a quarter of a million on the ~700 §5.1 projects for a full fan-out. Blocking
    keeps it linear in the number of businesses; ``geo.CELL_DEGREES`` is sized so
    a 3×3 neighbourhood cannot miss a pair inside the match radius.
    """
    # A business with either coordinate missing never enters the grid: §10.1's
    # tier 3 is an AND, so the distance half can never pass for it. Missing stays
    # missing rather than defaulting to "probably the same place".
    located: list[_Point] = [
        (b.id, float(b.lat), float(b.lng))
        for b in businesses
        if b.lat is not None and b.lng is not None
    ]

    cells: defaultdict[tuple[int, int], list[_Point]] = defaultdict(list)
    for point in located:
        cells[grid_cell(point[1], point[2])].append(point)

    pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for cell, members in cells.items():
        neighbours = [n for c in neighbouring_cells(cell) for n in cells.get(c, ())]
        for left_id, left_lat, left_lng in members:
            for right_id, right_lat, right_lng in neighbours:
                if left_id == right_id:
                    continue
                if is_near(left_lat, left_lng, right_lat, right_lng):
                    pairs.add(
                        (left_id, right_id) if left_id < right_id else (right_id, left_id)
                    )
    return sorted(pairs)


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #


def decide(
    left: Business,
    right: Business,
    tier: MatchTier,
    *,
    fuzzy_threshold: int,
    corroborated_threshold: int,
) -> Verdict:
    """Should these two rows become one?

    ``place_id`` is an identity assertion by Google and merges on its own. Every
    other tier is a *similarity*, and similarity without co-location is how a
    chain loses its branches: House of Salons publishes the same seven numbers on
    three Islamabad branches, and COSMO Salon the same seven across Gulberg and
    DHA. §10.1's phone and domain tiers, applied literally, merge each of those
    into one row and throw the other branches away.
    """
    if tier is MatchTier.PLACE_ID:
        return Verdict.MERGE

    if not is_near(
        _coord(left.lat), _coord(left.lng), _coord(right.lat), _coord(right.lng)
    ):
        return Verdict.REJECTED_DISTANCE

    # Checked before the ratio, because the ratio cannot see it: two branches of
    # one brand serving different clienteles score above even the strict
    # threshold, and merging them loses a separately-staffed premises.
    if conflicting_segments(left.name_norm, right.name_norm):
        return Verdict.REJECTED_SEGMENT

    threshold = (
        corroborated_threshold
        if tier in (MatchTier.EXACT_PHONE, MatchTier.DOMAIN)
        else fuzzy_threshold
    )
    ratio = token_set_ratio(left.name_norm, right.name_norm)
    return Verdict.MERGE if ratio >= threshold else Verdict.REJECTED_NAME


def _coord(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #


def _merge_group(session: Session, members: list[Business], report: DedupeReport) -> Business:
    """Fold a group of duplicates into its richest member, and return it."""
    survivor, *losers = sorted(members, key=_richness, reverse=True)
    merged_from = list(survivor.merged_from or [])

    for loser in losers:
        _fill_gaps(survivor, loser)
        merged_from.append(str(loser.id))
        merged_from.extend(loser.merged_from or [])

        # §10.1: "union all contacts … never discard a contact during merge".
        # Taken literally — every row is re-parented, including a number the
        # survivor already has. Two rows for one number are two provenance
        # records (§1), and folding them would drop whichever `source` lost,
        # which is the input §10.2's source_agreement counts. The operator never
        # sees the number twice because §3.3 ranking gives the slot to one row.
        result = cast(
            CursorResult,
            session.execute(
                update(Contact)
                .where(Contact.business_id == loser.id)
                .values(business_id=survivor.id)
                .execution_options(synchronize_session=False)
            ),
        )
        moved = result.rowcount
        report.contacts_moved += moved

        session.expire(loser, ["contacts"])
        session.delete(loser)
        report.businesses_absorbed += 1

    survivor.merged_from = merged_from
    session.expire(survivor, ["contacts"])
    report.groups_merged += 1
    return survivor


def _richness(business: Business) -> tuple:
    """Which row of a duplicate group should survive.

    Most contacts first — the survivor's ``id`` is the one that persists, and
    keeping the row that already carries the most evidence minimises what the
    merge has to move. ``created_at`` and ``id`` make it deterministic, so a
    re-run picks the same survivor and the stage stays idempotent.
    """
    populated = sum(
        1
        for name in (
            "address", "lat", "lng", "place_id", "website",
            "facebook_url", "instagram_url", "rating", "review_count", "area",
        )
        if getattr(business, name) is not None
    )
    return (len(business.contacts), populated, -business.created_at.timestamp(), str(business.id))


_GAP_FILL_FIELDS = (
    "address", "area", "city", "category", "subcategory", "lat", "lng",
    "place_id", "website", "facebook_url", "instagram_url", "rating", "review_count",
)


def _fill_gaps(survivor: Business, loser: Business) -> None:
    """§10.1: "keep the highest-confidence value per field". Only ever fills.

    The same rule ``ingest`` applies across queries: a merge must not blank a
    field the survivor already has, and must not silently prefer the loser's
    value where both are populated.
    """
    for name in _GAP_FILL_FIELDS:
        if getattr(survivor, name) is None:
            value = getattr(loser, name)
            if value is not None:
                setattr(survivor, name, value)


def _rescore_survivors(
    session: Session, run: Run, survivors: list[Business], report: DedupeReport
) -> None:
    """§10.2 and §3.3 again, for the rows a merge changed.

    §2 numbers scoring as Stage 5 and dedupe as Stage 6, which means a merge
    lands *after* the scores. A survivor that just absorbed another business has
    a different contact set, a different ``n_sources`` and a different
    completeness, so its Stage 5 answer is stale the moment it merges. Re-running
    the scorer over just the survivors keeps §2's stage order and the table
    correct at the same time.
    """
    if not survivors:
        return
    for survivor in survivors:
        report.duplicate_numbers_after_merge += _duplicate_numbers(survivor)
    score_run(session, run, businesses=survivors)
    report.rescored = len(survivors)


def _duplicate_numbers(business: Business) -> int:
    numbers = [
        c.value_e164 for c in business.contacts
        if c.kind == ContactKind.PHONE and c.value_e164
    ]
    return len(numbers) - len(set(numbers))


def _finalise(report: DedupeReport) -> None:
    if report.businesses_absorbed:
        log.info(
            "dedupe.merged",
            groups=report.groups_merged,
            absorbed=report.businesses_absorbed,
            contacts_moved=report.contacts_moved,
        )
    if report.rejected_distance:
        # Not a failure — this is the count of chain branches the cascade
        # correctly declined to destroy. Logged because it is the number §10.1's
        # literal reading would have merged.
        log.info(
            "dedupe.rejected_by_distance",
            pairs=report.rejected_distance,
            radius_m=NEAR_DUPLICATE_METRES,
        )


# --------------------------------------------------------------------------- #
# Union-find
# --------------------------------------------------------------------------- #


class _Union:
    """Merges are transitive: if A ~ B and B ~ C then all three are one business."""

    def __init__(self) -> None:
        self._parent: dict[uuid.UUID, uuid.UUID] = {}

    def find(self, item: uuid.UUID) -> uuid.UUID:
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: uuid.UUID, right: uuid.UUID) -> bool:
        """Returns ``True`` if this call actually joined two distinct groups."""
        a, b = self.find(left), self.find(right)
        if a == b:
            return False
        self._parent[a] = b
        return True

    def groups(self) -> list[list[uuid.UUID]]:
        buckets: defaultdict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for item in self._parent:
            buckets[self.find(item)].append(item)
        return [sorted(members, key=str) for members in buckets.values() if len(members) > 1]

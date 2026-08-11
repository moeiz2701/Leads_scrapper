"""§13 Screen 1's "Est. runtime · Est. available", without lying.

§13 wants both numbers before the operator commits an hour. §5.2 forbids one of
them: *"Measure per slice; do not extrapolate one run's confirmation rate into
the §13 estimated-available figure."* Islamabad and Lahore ran identical code on
the same category and confirmed WhatsApp at **45% against 13%**.

The resolution is that the two halves of that line are not the same kind of
question:

* **Runtime is ours.** It falls out of the query plan and our own §7 pacing —
  properties of this system, measurable from our own history, and true whatever
  the market holds. Estimated, as a range.
* **Availability is the market's.** How many salons exist in Faisalabad is a fact
  about Faisalabad that no amount of arithmetic on a Lahore run reveals. §14 says
  the honest figure for a narrow pair is "30–50, not several hundred", and §13
  says to surface that rather than pad the table.

So this module **reports prior measured outcomes for the exact slice and refuses
to extrapolate**. Where the slice has been run before, the operator sees what it
actually produced. Where it has not, they see "no prior run" — which is the true
answer, and a more useful one than a fabricated `~780`.

The measured spread that settles it, from the six runs in the database — same
category, three cities, unique businesses per Maps query:

| Islamabad × salon | Lahore × salon | Karachi × salon |
|---|---|---|
| 66 / query | 20 / query | 19.5 / query |

A 3.4× spread *within one category*. Any single multiplier here is wrong for two
of the three cities. The two Lahore runs also disagree with each other in the
wrong direction — 3 queries returned 60 unique and 6 queries returned 52 — so the
data does not even support a monotonic curve to fit, let alone a point estimate.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from leadscraper.config import get_settings
from leadscraper.db.models import Run
from leadscraper.enums import Category, RunStatus
from leadscraper.taxonomy import build_query_plan

# §14's worked example: 60 Maps queries in 12 minutes. Used only as a fallback
# when this installation has no live run of its own to measure, and labelled as
# such — it is a figure from the doc, not from this machine.
DOC_SECONDS_PER_QUERY = 12.0

# §5.2 measured the Islamabad website pass at "~5 minutes" for 62 domains.
DOC_SECONDS_PER_DOMAIN = 5.0

# §14: 32% of discovered businesses carry a real website — the one assumption in
# that table measured end to end. Used to size the enrichment stage's input, not
# to predict its yield.
WEBSITE_FILL_RATE = 0.32

# §6 Stage 3. Both figures are measured **on this installation**, not taken from
# the doc — §6 has no throughput numbers at all, and §6.7 found that the tier
# needs a rendered browser rather than the plain page load §6.4 assumes.
#
# Median render-to-render gap across 127 live renders (mean 17.9, p90 31.5):
# §6.6's mandated 8–20 s delay plus a browser launch and a page load. This term
# dominates any run with the social toggles on — Lahore × food is ~45 minutes of
# social against ~12 of discovery — so omitting it does not make Screen 1
# slightly optimistic, it makes it wrong.
SOCIAL_SECONDS_PER_BUSINESS = 19.0

# 248 of the 898 businesses across the seven runs carry a Facebook or Instagram
# URL. Per slice it runs 19–33%, which is real spread and is why the caveat below
# says so out loud rather than presenting 28% as a property of the market.
SOCIAL_FILL_RATE = 0.28


@dataclass(frozen=True, slots=True)
class Range:
    low: float
    high: float

    def as_dict(self) -> dict[str, float]:
        return {"low": round(self.low, 1), "high": round(self.high, 1)}


@dataclass(frozen=True, slots=True)
class PriorRun:
    """What this exact city × category actually produced, last time it was run."""

    run_id: str
    status: str
    queries: int
    businesses: int
    with_phone: int
    qualified: int
    enriched: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "queries": self.queries,
            "businesses": self.businesses,
            "with_phone": self.with_phone,
            "qualified": self.qualified,
            "enriched": self.enriched,
        }


@dataclass(slots=True)
class RunEstimate:
    queries: int
    runtime_minutes: Range | None = None
    runtime_basis: str = "none"
    prior_runs: list[PriorRun] = field(default_factory=list)
    available: Range | None = None
    available_basis: str = "no_prior_run"
    qualified: Range | None = None
    caveats: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "queries": self.queries,
            "runtime_minutes": self.runtime_minutes.as_dict() if self.runtime_minutes else None,
            "runtime_basis": self.runtime_basis,
            "prior_runs": [p.as_dict() for p in self.prior_runs],
            "available": self.available.as_dict() if self.available else None,
            "available_basis": self.available_basis,
            "qualified": self.qualified.as_dict() if self.qualified else None,
            "caveats": self.caveats,
        }


def estimate_run(
    session: Session,
    city: str,
    category: Category | str,
    *,
    synonym_limit: int | None = None,
    tile_limit: int | None = None,
    enrich: bool = True,
    social: bool = False,
) -> RunEstimate:
    """Everything §13 Screen 1 can honestly say before the operator clicks Start."""
    plan = build_query_plan(city, category, synonym_limit, tile_limit)
    estimate = RunEstimate(queries=len(plan))

    priors = _prior_runs(session, city, category)
    estimate.prior_runs = priors

    _estimate_runtime(session, estimate, priors, enrich=enrich, social=social)
    _report_available(estimate, priors)
    return estimate


# --------------------------------------------------------------------------- #
# Runtime — ours to estimate
# --------------------------------------------------------------------------- #


def _estimate_runtime(
    session: Session,
    estimate: RunEstimate,
    priors: list[PriorRun],
    *,
    enrich: bool,
    social: bool = False,
) -> None:
    per_query, basis = _seconds_per_query(session)
    estimate.runtime_basis = basis

    discovery_low = estimate.queries * per_query.low
    discovery_high = estimate.queries * per_query.high

    # Sized from a prior run of the exact slice, for both post-discovery stages:
    # otherwise the business count is exactly the unknown this module refuses to
    # invent, and every term downstream of it inherits that.
    businesses = max((p.businesses for p in priors), default=0)
    unsized: list[str] = []

    enrich_low = enrich_high = 0.0
    if enrich:
        if businesses:
            domains = businesses * WEBSITE_FILL_RATE
            enrich_low = domains * DOC_SECONDS_PER_DOMAIN * 0.6
            enrich_high = domains * DOC_SECONDS_PER_DOMAIN * 1.4
        else:
            unsized.append("website pass")

    social_low = social_high = 0.0
    if social:
        if businesses:
            # §6.6 pins concurrency at 1, so this is wall clock and it does not
            # overlap anything: profiles = businesses × the share carrying a
            # social URL, each costing one browser render.
            profiles = businesses * SOCIAL_FILL_RATE
            social_low = profiles * SOCIAL_SECONDS_PER_BUSINESS * 0.6
            social_high = profiles * SOCIAL_SECONDS_PER_BUSINESS * 1.4
            estimate.caveats.append(
                "The §6 social pass is the dominant term when it is on: §6.6 caps "
                "it at concurrency 1 with an 8-20 s delay and §6.7 measured that "
                "each profile needs a rendered browser. Sized at 28% of businesses "
                "carrying a social URL, which ranged 19-33% across the seven runs."
            )
        else:
            unsized.append("social pass")

    if unsized:
        estimate.caveats.append(
            f"Runtime covers discovery only — sizing the {' and the '.join(unsized)} "
            f"needs a business count, and this slice has never been run."
        )

    estimate.runtime_minutes = Range(
        low=(discovery_low + enrich_low + social_low) / 60.0,
        high=(discovery_high + enrich_high + social_high) / 60.0,
    )

    if basis == "doc_projection":
        estimate.caveats.append(
            "Runtime is §14's published 12 s/query — no live discovery run on "
            "this installation to measure against yet."
        )
    if get_settings().proxy_mode == "direct":
        # §7.1: every measurement in this project was taken on a direct
        # connection. A residential PK proxy adds a hop to every request.
        estimate.caveats.append(
            "Measured without a PK residential proxy (§7.1). A proxied run will "
            "be slower, and Maps will geo-rank differently."
        )


def _seconds_per_query(session: Session) -> tuple[Range, str]:
    """Measured from this installation's own live discovery runs.

    Only runs that actually issued queries count. A run served entirely from the
    §7 cache finished in 0.4 s and would drag the floor to something no live run
    can hit — the cache is the reason to *not* believe that number, not a reason
    to publish it.
    """
    samples: list[float] = []
    for run in session.execute(select(Run)).scalars():
        stats = (run.stats or {}).get("discovery") or {}
        queries = stats.get("queries_run") or 0
        elapsed = stats.get("elapsed_seconds")
        if elapsed is None and run.started_at and run.finished_at:
            # Older runs predate stage timing. Run-level wall clock is a fair
            # proxy for a discovery-only run and an overestimate otherwise,
            # which errs in the safe direction for an estimate.
            elapsed = (run.finished_at - run.started_at).total_seconds()
        if queries >= 2 and elapsed and elapsed > 1.0:
            samples.append(elapsed / queries)

    if not samples:
        return Range(DOC_SECONDS_PER_QUERY * 0.8, DOC_SECONDS_PER_QUERY * 1.25), "doc_projection"
    if len(samples) == 1:
        return Range(samples[0] * 0.7, samples[0] * 1.5), "measured_single_run"
    return Range(min(samples), max(samples)), "measured"


# --------------------------------------------------------------------------- #
# Availability — the market's to reveal
# --------------------------------------------------------------------------- #


def _prior_runs(session: Session, city: str, category: Category | str) -> list[PriorRun]:
    key = category.value if isinstance(category, Category) else str(category)
    statement = (
        select(Run)
        .where(Run.city == city, Run.category == key)
        .order_by(Run.created_at.desc())
    )
    priors: list[PriorRun] = []
    for run in session.execute(statement).scalars():
        if run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
            continue
        stats = run.stats or {}
        discovery = stats.get("discovery") or {}
        scoring = stats.get("normalise_score") or {}
        priors.append(
            PriorRun(
                run_id=str(run.id),
                status=run.status,
                queries=int(discovery.get("queries_planned") or 0),
                businesses=int(discovery.get("created") or scoring.get("businesses_total") or 0),
                with_phone=int(scoring.get("with_phone") or 0),
                qualified=int(scoring.get("qualified") or 0),
                enriched="website_enrichment" in stats,
            )
        )
    return priors


def _report_available(estimate: RunEstimate, priors: list[PriorRun]) -> None:
    """Report what the slice has produced. Never project what it might.

    Two separate refusals, because they fail for different reasons:

    * With no prior run there is no basis at all.
    * With prior runs that were never enriched, the business count is real but the
      *qualified* count is structurally 0 — §10.2 measured three discovery-only
      Lahore runs at 0 qualified against the enriched one's 22, because every
      Maps number is a §9.3 ``likely`` at 0.60 and nothing lifts it over 60.
      Reporting that 0 as a forecast would read as "this city has no leads".
    """
    if not priors:
        estimate.available_basis = "no_prior_run"
        estimate.caveats.append(
            "No prior run for this city × category, so there is no honest "
            "availability figure. §14: a narrow pair is genuinely 30–50, not "
            "several hundred — the run itself is the measurement."
        )
        return

    counts = [p.businesses for p in priors if p.businesses]
    if counts:
        estimate.available = Range(min(counts), max(counts))
        estimate.available_basis = "measured_this_slice"
        queries = sorted({p.queries for p in priors if p.queries})
        if queries:
            estimate.caveats.append(
                f"Measured from {len(priors)} prior run(s) at "
                f"{queries[0]}–{queries[-1]} queries. Not scaled to this plan's "
                f"{estimate.queries}: §14 measured a 67% duplicate rate across "
                "near-synonyms, so unique yield saturates rather than growing "
                "with the query count."
            )

    enriched = [p for p in priors if p.enriched]
    if enriched:
        qualified = [p.qualified for p in enriched]
        estimate.qualified = Range(min(qualified), max(qualified))
    else:
        estimate.caveats.append(
            "No prior run of this slice was enriched, so there is no qualified "
            "figure. A discovery-only run yields 0 qualified leads by "
            "construction (§10.2) — that is a property of the pipeline, not of "
            "the city."
        )


def slice_confirmation_rates(session: Session) -> dict[str, float]:
    """Per-slice WhatsApp confirmation rate. Diagnostic, never an input.

    Exposed so the §13 Settings screen can *show* the 45%/13% spread that makes
    extrapolation dishonest, rather than the operator having to take §5.2's word
    for it. Deliberately not consumed by ``estimate_run``.
    """
    rates: dict[str, float] = {}
    for run in session.execute(select(Run)).scalars():
        web = (run.stats or {}).get("website_enrichment") or {}
        crawled = web.get("domains_crawled") or 0
        if not crawled:
            continue
        key = f"{run.city} × {run.category}"
        rates.setdefault(key, round((web.get("confirmed_whatsapp") or 0) / crawled, 3))
    return rates


def spread(values: list[float]) -> float:
    """Max/min ratio — how badly a single multiplier would fit."""
    usable = [v for v in values if v > 0]
    if len(usable) < 2:
        return 1.0
    return max(usable) / min(usable)


def mean_or_none(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None

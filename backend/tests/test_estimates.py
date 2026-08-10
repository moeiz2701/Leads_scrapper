"""§13 Screen 1's estimate line, and what it refuses to say.

§5.2: *"Measure per slice; do not extrapolate one run's confirmation rate into
the §13 estimated-available figure."* These tests pin the refusal, because the
pressure to print a plausible number where the mockup shows one is exactly how
the rule gets quietly broken.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from leadscraper.db.models import Run
from leadscraper.enums import NumberPreference, RunStatus
from leadscraper.services.estimates import estimate_run, spread
from tests.conftest import requires_db


def _run(
    session: Session,
    city: str,
    category: str = "salon",
    *,
    businesses: int = 0,
    queries: int = 3,
    qualified: int = 0,
    enriched: bool = False,
) -> Run:
    stats: dict = {
        "discovery": {
            "queries_planned": queries,
            "queries_run": queries,
            "created": businesses,
        },
        "normalise_score": {
            "businesses_total": businesses,
            "qualified": qualified,
            "with_phone": businesses,
        },
    }
    if enriched:
        stats["website_enrichment"] = {"domains_crawled": 20, "confirmed_whatsapp": 9}
    run = Run(
        city=city,
        category=category,
        number_pref=NumberPreference.OWNER_FIRST,
        sources_enabled={"google_maps": True},
        status=RunStatus.DONE,
        stats=stats,
    )
    session.add(run)
    session.flush()
    return run


# --------------------------------------------------------------------------- #
# The refusal
# --------------------------------------------------------------------------- #


@requires_db
def test_a_slice_never_run_gets_no_availability_number(db_session: Session):
    """§14: a narrow pair is honestly 30–50, not several hundred.

    The only truthful answer before the first run is that we do not know, and
    §13 would rather show that than have the operator wait an hour on a number
    we invented.
    """
    estimate = estimate_run(db_session, "Faisalabad", "entertainment")

    assert estimate.available is None
    assert estimate.available_basis == "no_prior_run"
    assert any("no prior run" in c.lower() for c in estimate.caveats)


@requires_db
def test_availability_is_never_extrapolated_from_a_different_city(db_session: Session):
    """The measured spread is 3.4× within one category — Islamabad 66 businesses
    per query against Karachi's 19.5. Any single multiplier is wrong for two of
    the three cities."""
    _run(db_session, "Islamabad", businesses=199, queries=3, qualified=45, enriched=True)

    estimate = estimate_run(db_session, "Karachi", "salon")

    assert estimate.available is None, "an Islamabad run says nothing about Karachi"
    assert estimate.available_basis == "no_prior_run"


@requires_db
def test_availability_reports_the_slices_own_measured_outcome(db_session: Session):
    _run(db_session, "Lahore", businesses=60, queries=3)
    _run(db_session, "Lahore", businesses=52, queries=8)

    estimate = estimate_run(db_session, "Lahore", "salon")

    assert estimate.available_basis == "measured_this_slice"
    assert (estimate.available.low, estimate.available.high) == (52, 60)


@requires_db
def test_measured_availability_is_not_scaled_up_to_a_bigger_query_plan(
    db_session: Session,
):
    """§14 measured a 67% duplicate rate across near-synonyms, so unique yield
    saturates. The two Lahore runs disagree in the wrong direction anyway — 3
    queries gave 60 unique and 6 gave 52 — so there is no curve to fit."""
    _run(db_session, "Lahore", businesses=60, queries=3)

    estimate = estimate_run(db_session, "Lahore", "salon")

    assert estimate.available.high == 60, "not multiplied by the planned query count"
    assert estimate.queries > 3, "the full plan is larger than the measured run"
    assert any("saturates" in c for c in estimate.caveats)


@requires_db
def test_discovery_only_history_yields_no_qualified_forecast(db_session: Session):
    """§10.2: three unenriched Lahore runs scored 0 qualified against the
    enriched one's 22. Publishing that 0 as a forecast would read as "this city
    has no leads" rather than "this pipeline was not finished"."""
    _run(db_session, "Multan", businesses=48, qualified=0, enriched=False)

    estimate = estimate_run(db_session, "Multan", "salon")

    assert estimate.available is not None, "the business count is real"
    assert estimate.qualified is None, "the qualified count is structurally zero"
    assert any("discovery-only" in c for c in estimate.caveats)


@requires_db
def test_qualified_is_reported_only_from_enriched_history(db_session: Session):
    _run(db_session, "Islamabad", businesses=199, qualified=45, enriched=True)

    estimate = estimate_run(db_session, "Islamabad", "salon")

    assert (estimate.qualified.low, estimate.qualified.high) == (45, 45)


# --------------------------------------------------------------------------- #
# Runtime — the half that *is* ours to estimate
# --------------------------------------------------------------------------- #


@requires_db
def test_runtime_is_estimated_even_for_an_unseen_slice(db_session: Session):
    """Runtime falls out of the query plan and our own §7 pacing. It is a fact
    about this system, not about how many salons Faisalabad has."""
    estimate = estimate_run(db_session, "Faisalabad", "salon")

    assert estimate.runtime_minutes is not None
    assert estimate.runtime_minutes.low > 0
    assert estimate.runtime_minutes.high >= estimate.runtime_minutes.low
    assert estimate.queries > 0


@requires_db
def test_runtime_is_a_range_and_says_where_it_came_from(db_session: Session):
    estimate = estimate_run(db_session, "Lahore", "salon")

    assert estimate.runtime_basis in {"doc_projection", "measured_single_run", "measured"}
    assert estimate.runtime_minutes.high > estimate.runtime_minutes.low


@requires_db
def test_estimate_serialises_without_losing_its_caveats(db_session: Session):
    """The caveats are the honest part; an API shape that drops them would put
    the bare number back on screen."""
    payload = estimate_run(db_session, "Quetta", "salon").as_dict()

    assert payload["available"] is None
    assert payload["available_basis"] == "no_prior_run"
    assert payload["caveats"]


def test_spread_measures_how_badly_one_multiplier_would_fit():
    """The 3.4× that settles the whole design: 66 / 19.5 per-query yield."""
    assert round(spread([66.0, 20.0, 19.5]), 1) == 3.4
    assert spread([10.0]) == 1.0

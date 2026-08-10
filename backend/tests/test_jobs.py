"""§2's queue consumers — what a run does, and what it refuses to call ``done``.

The rule these tests exist to protect is §5.5's, applied at the stage boundary:
**a run that could not do its work must not report ``done``.** The project has
been bitten three times by a source silently returning zero, and a green status
on an incomplete run is the same defect one layer up.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from leadscraper.db.models import Run, SourceState
from leadscraper.enums import NumberPreference, RunStatus, SourceStatus, Stage
from leadscraper.pipeline.jobs import (
    STATS_KEY,
    RunCancelled,
    begin_stage,
    close_run,
    fail_stage,
    finish_stage,
    planned_stages,
    unavailable_stages,
)
from leadscraper.pipeline.stages import (
    IMPLEMENTED_STAGES,
    StageNotImplementedError,
    StageResult,
)
from tests.conftest import requires_db

ALL_CORE = {"google_maps": True, "business_website": True}


def _run(session: Session, **overrides) -> Run:
    base = dict(
        city="Islamabad",
        category="salon",
        number_pref=NumberPreference.OWNER_FIRST,
        sources_enabled=dict(ALL_CORE),
        status=RunStatus.QUEUED,
    )
    run = Run(**{**base, **overrides})
    session.add(run)
    session.flush()
    return run


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


def test_planned_stages_are_the_implemented_ones_in_section_2_order():
    assert planned_stages(ALL_CORE) == [
        Stage.DISCOVERY,
        Stage.CONTACT_ENRICHMENT,
        Stage.NORMALISE_SCORE,
        Stage.DEDUPE_EXPORT,
    ]


def test_social_stage_is_never_planned_while_its_body_is_missing():
    """Phase 8. Enqueuing it would create work that can only raise."""
    plan = planned_stages({**ALL_CORE, "facebook": True})
    assert Stage.SOCIAL_ENRICHMENT not in plan
    assert unavailable_stages({"facebook": True}) == [Stage.SOCIAL_ENRICHMENT]


def test_every_planned_stage_has_an_implementation():
    """The invariant the API relies on to refuse a run with a clear message."""
    assert set(planned_stages(ALL_CORE)) <= IMPLEMENTED_STAGES


def test_disabling_maps_drops_discovery_but_keeps_scoring():
    """§3.2's seed mode enters at Stage 2; the scoring pass is unconditional."""
    plan = planned_stages({"google_maps": False, "business_website": True})
    assert Stage.DISCOVERY not in plan
    assert Stage.NORMALISE_SCORE in plan


def test_every_stage_has_a_stats_key():
    """§13 Screen 2 reads ``runs.stats`` per stage; a missing key is a blank row."""
    assert set(STATS_KEY) == set(Stage)


# --------------------------------------------------------------------------- #
# Status transitions
# --------------------------------------------------------------------------- #


@requires_db
def test_a_run_with_no_missing_stages_closes_as_done(db_session: Session):
    run = _run(db_session, stats={"stages_completed": ["discovery"]})
    close_run(run)
    assert run.status == RunStatus.DONE
    assert run.finished_at is not None


@requires_db
def test_a_run_that_skipped_a_stage_closes_as_partial_not_done(db_session: Session):
    """§5.5 — ``partial`` is not a lesser ``done``, it is a different fact.

    A green "done" on a run that never ran a stage the operator asked for is
    exactly how you harvest nothing and do not notice.
    """
    run = _run(db_session, stats={"unimplemented": {"social_enrichment": "Phase 8"}})
    close_run(run)
    assert run.status == RunStatus.PARTIAL


@requires_db
def test_a_single_stage_rerun_leaves_the_run_terminal_not_running(db_session: Session):
    """The mirror image of §5.5, found by running it.

    A ``chain=False`` re-run — §2's whole point, and what the API does when the
    operator changes ``number_preference`` — has nothing downstream coming to
    close it, so the run sat at ``running`` for ever and §13 Screen 2 showed a
    spinner on a run that had finished seconds earlier.
    """
    run = _run(db_session, status=RunStatus.RUNNING)
    close_run(run)

    assert run.status == RunStatus.DONE
    assert run.finished_at is not None


@requires_db
def test_a_failed_stage_marks_the_run_failed_and_records_why(db_session: Session):
    run = _run(db_session)
    fail_stage(run, Stage.DISCOVERY, RuntimeError("selector drifted"), elapsed=3.2)

    assert run.status == RunStatus.FAILED
    assert "selector drifted" in run.error
    assert run.stats["failures"]["discovery"]["elapsed_seconds"] == 3.2


@requires_db
def test_a_cancelled_run_refuses_to_start_its_next_stage(db_session: Session):
    """Cancel stops the chain at the next stage boundary."""
    run = _run(db_session, status=RunStatus.CANCELLED)
    with pytest.raises(RunCancelled):
        begin_stage(run, Stage.NORMALISE_SCORE)


@requires_db
def test_beginning_a_stage_records_it_as_current(db_session: Session):
    run = _run(db_session)
    begin_stage(run, Stage.DISCOVERY)

    assert run.status == RunStatus.RUNNING
    assert run.stats["current_stage"] == "discovery"
    assert run.started_at is not None


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #


@requires_db
def test_finishing_a_stage_adds_timing_without_clobbering_its_report(
    db_session: Session,
):
    """Phases 2–4 already write rich reports under these keys. The job wrapper
    merges timing in; overwriting would destroy the counters the run summary and
    §13 Screen 2 both read."""
    run = _run(
        db_session,
        stats={"discovery": {"created": 199, "queries_run": 3, "raw_results": 249}},
    )
    result = StageResult(stage=Stage.DISCOVERY, run_id=run.id, processed=3, produced=199)

    finish_stage(db_session, run, Stage.DISCOVERY, elapsed=43.7, result=result)

    discovery = run.stats["discovery"]
    assert discovery["created"] == 199, "the stage's own report survived"
    assert discovery["queries_run"] == 3
    assert discovery["elapsed_seconds"] == 43.7


@requires_db
def test_stage_timing_is_recorded_so_the_estimator_has_something_to_measure(
    db_session: Session,
):
    """§13 Screen 1's runtime estimate had no per-stage timing to work from —
    the six runs in the database carry only a run-level wall clock that includes
    every re-run of every stage."""
    run = _run(db_session)
    result = StageResult(stage=Stage.NORMALISE_SCORE, run_id=run.id)

    finish_stage(db_session, run, Stage.NORMALISE_SCORE, elapsed=1.4, result=result)

    assert run.stats["normalise_score"]["elapsed_seconds"] == 1.4
    assert "normalise_score" in run.stats["stages_completed"]


@requires_db
def test_an_unimplemented_stage_is_recorded_by_phase_not_as_a_crash(
    db_session: Session,
):
    run = _run(db_session)
    exc = StageNotImplementedError(Stage.SOCIAL_ENRICHMENT, "Phase 8")

    finish_stage(
        db_session, run, Stage.SOCIAL_ENRICHMENT, elapsed=0.0, unimplemented=str(exc)
    )
    close_run(run)

    assert "Phase 8" in run.stats["unimplemented"]["social_enrichment"]
    assert run.status == RunStatus.PARTIAL


# --------------------------------------------------------------------------- #
# §13 Screen 2's source pills
# --------------------------------------------------------------------------- #


@requires_db
def test_source_state_is_written_so_the_pills_have_a_writer(db_session: Session):
    """``BreakerRegistry`` is in-process and dies with the worker, so
    ``source_state`` had a table and no writer — empty since Phase 1. The stage
    reports carry the facts the breaker acted on, so they are projected here."""
    run = _run(db_session, stats={"discovery": {"queries_run": 3, "queries_failed": 0}})

    finish_stage(
        db_session,
        run,
        Stage.DISCOVERY,
        elapsed=10.0,
        result=StageResult(stage=Stage.DISCOVERY, run_id=run.id),
    )

    state = (
        db_session.query(SourceState)
        .filter_by(run_id=run.id, source="google_maps")
        .one()
    )
    assert state.status == SourceStatus.OK


@requires_db
def test_a_refusing_host_shows_as_throttled_not_blocked(db_session: Session):
    """§5.2 — "one host refusing is not the source refusing".

    A single 403 from one salon's WAF once tripped a source-level breaker and
    skipped 19 healthy domains, turning a 61%-yield run into a 23% one. The pill
    must not repeat that conflation.
    """
    run = _run(
        db_session,
        stats={"website_enrichment": {"sites_ok": 40, "sites_refused": 2}},
    )

    finish_stage(
        db_session,
        run,
        Stage.CONTACT_ENRICHMENT,
        elapsed=60.0,
        result=StageResult(stage=Stage.CONTACT_ENRICHMENT, run_id=run.id),
    )

    state = (
        db_session.query(SourceState)
        .filter_by(run_id=run.id, source="business_website")
        .one()
    )
    assert state.status == SourceStatus.THROTTLED
    assert "refused" in state.last_error


@requires_db
def test_a_blocked_source_shows_as_blocked(db_session: Session):
    run = _run(db_session, stats={"discovery": {"queries_blocked": 4}})

    finish_stage(
        db_session,
        run,
        Stage.DISCOVERY,
        elapsed=5.0,
        result=StageResult(stage=Stage.DISCOVERY, run_id=run.id),
    )

    state = db_session.query(SourceState).filter_by(run_id=run.id).one()
    assert state.status == SourceStatus.BLOCKED


@requires_db
def test_pure_database_stages_get_no_source_pill(db_session: Session):
    """Stages 5 and 6 make no requests. A green pill for them would be noise."""
    run = _run(db_session, stats={"normalise_score": {"scored": 199}})

    finish_stage(
        db_session,
        run,
        Stage.NORMALISE_SCORE,
        elapsed=1.0,
        result=StageResult(stage=Stage.NORMALISE_SCORE, run_id=run.id),
    )

    assert db_session.query(SourceState).filter_by(run_id=run.id).count() == 0

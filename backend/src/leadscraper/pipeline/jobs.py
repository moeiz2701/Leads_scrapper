"""What an RQ worker actually executes, and how a run moves through §2's stages.

Phase 1 built the queues; nothing enqueued and nothing consumed. This is the
consumer side.

**Why a queue at all, when a full Islamabad run takes five minutes.** §2 opens
with "do not build this as one browser script … you need to re-run stage 3
without re-running stage 1", and that is the deciding argument: a stage is the
unit of retry, and re-running one is a normal operation, not a recovery. Three
more follow from it — a five-minute HTTP POST is a timeout on any proxy between
the browser and here; §13 Screen 2 wants counters *while* work is in flight,
which a blocking request cannot produce; and Cancel needs something to cancel.

**Stages chain themselves rather than using RQ's ``depends_on``.** Each job
enqueues its successor on success. That keeps the failure semantics here, in
code that knows what §5.5 means — a stage that could not do its work must stop
the chain rather than let the next stage run on nothing and report ``done``.

**One flag collapses this to the synchronous mode.** ``queue_sync`` makes the
queues inline (RQ's ``is_async=False``), so the self-chaining runs the whole
pipeline inside the caller. Tests use it, and so can a machine with no worker
running; the API code path is identical either way, so the two modes cannot
drift.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from leadscraper.db.models import Run, SourceState
from leadscraper.db.session import session_scope
from leadscraper.enums import RunStatus, SourceStatus, Stage
from leadscraper.logging import get_logger
from leadscraper.pipeline.stages import (
    IMPLEMENTED_STAGES,
    STAGE_FUNCTIONS,
    StageNotImplementedError,
    StageResult,
)

log = get_logger(__name__)

# The §2 pipeline in order. A run executes the sub-sequence its `sources_enabled`
# and the implemented set allow, but never out of this order.
STAGE_ORDER: tuple[Stage, ...] = tuple(Stage)

# Where each stage writes its report inside `runs.stats`. These keys are already
# in the live data (Phases 2–4 wrote them), so they are read from here rather
# than re-derived — the §13 Screen 2 counters must find the existing runs' stats,
# not just the ones this module creates.
STATS_KEY: dict[Stage, str] = {
    Stage.DISCOVERY: "discovery",
    Stage.CONTACT_ENRICHMENT: "website_enrichment",
    Stage.SOCIAL_ENRICHMENT: "social_enrichment",
    Stage.PERSON_ATTRIBUTION: "person_attribution",
    Stage.NORMALISE_SCORE: "normalise_score",
    Stage.DEDUPE_EXPORT: "dedupe",
}


class RunCancelled(Exception):
    """The operator pressed Cancel before this stage started."""


def planned_stages(sources_enabled: dict, *, enrich: bool = True) -> list[Stage]:
    """The stages a run will actually execute, in §2 order.

    Filtered against ``IMPLEMENTED_STAGES`` so the API can tell the operator what
    a run will and will not do *before* it starts, rather than enqueuing work
    that can only raise. Stages 3 and 4 are Phases 8 and 9 and drop out here.
    """
    stages = [Stage.DISCOVERY] if sources_enabled.get("google_maps", True) else []
    # Stage 2 has two inputs — §5.2 business websites and §5.3 directories — and
    # either one on its own is a reason to run it. §2 lists directories under
    # contact enrichment for exactly this reason.
    if enrich and (
        sources_enabled.get("business_website", True) or sources_enabled.get("directories")
    ):
        stages.append(Stage.CONTACT_ENRICHMENT)
    if sources_enabled.get("facebook") or sources_enabled.get("instagram"):
        stages.append(Stage.SOCIAL_ENRICHMENT)
    stages.extend([Stage.NORMALISE_SCORE, Stage.DEDUPE_EXPORT])
    ordered = [s for s in STAGE_ORDER if s in set(stages)]
    return [s for s in ordered if s in IMPLEMENTED_STAGES]


def unavailable_stages(sources_enabled: dict) -> list[Stage]:
    """Stages the operator asked for that have no body yet.

    Surfaced by the API so a request for Facebook comes back as "Phase 8 builds
    that" instead of a run that silently omits it — §5.5's failure mode is not
    noticing that a source produced nothing.
    """
    requested: list[Stage] = []
    if sources_enabled.get("facebook") or sources_enabled.get("instagram"):
        requested.append(Stage.SOCIAL_ENRICHMENT)
    return [s for s in requested if s not in IMPLEMENTED_STAGES]


# --------------------------------------------------------------------------- #
# The job
# --------------------------------------------------------------------------- #


def run_stage_job(run_id: str | uuid.UUID, stage_value: str, *, chain: bool = True) -> dict:
    """Execute one stage for one run. **This is what the worker runs.**

    Arguments are primitives because RQ serialises them into Redis and a job
    argument that needs the ORM to deserialise is a job that cannot be inspected
    with ``rq info``.
    """
    identifier = uuid.UUID(str(run_id))
    stage = Stage(stage_value)
    started = time.monotonic()

    try:
        _begin(identifier, stage)
    except RunCancelled:
        log.info("stage.skipped_cancelled", run_id=str(identifier), stage=stage.value)
        return {"stage": stage.value, "skipped": "cancelled"}

    try:
        result: StageResult = STAGE_FUNCTIONS[stage](identifier)
    except StageNotImplementedError as exc:
        # Not a crash — a stage whose phase has not landed. The run is `partial`
        # rather than `failed`, and it says which phase, because the distinction
        # is what tells the operator whether to retry or to wait for a release.
        _finish(identifier, stage, elapsed=time.monotonic() - started, unimplemented=str(exc))
        log.warning("stage.not_implemented", run_id=str(identifier), stage=stage.value)
        _settle(identifier, stage, chain=chain)
        return {"stage": stage.value, "unimplemented": str(exc)}
    except Exception as exc:  # noqa: BLE001 — recorded on the run, then re-raised
        _fail(identifier, stage, exc, elapsed=time.monotonic() - started)
        raise

    _finish(identifier, stage, elapsed=time.monotonic() - started, result=result)
    _settle(identifier, stage, chain=chain)
    return result.as_dict()


def _settle(run_id: uuid.UUID, stage: Stage, *, chain: bool) -> None:
    """Hand on to the next stage, or bring the run to a terminal status.

    The ``chain=False`` branch is not a detail: a single-stage re-run (§2's whole
    point, and what the API does when the operator changes ``number_preference``)
    left the run at ``running`` for ever, because nothing downstream was coming
    to close it. §13 Screen 2 would show a spinner on a run that finished
    seconds ago — the mirror image of §5.5's rule, and just as misleading.
    """
    if chain:
        _enqueue_next(run_id, stage)
        return

    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is not None and run.status == RunStatus.RUNNING:
            close_run(run)


def _begin(run_id: uuid.UUID, stage: Stage) -> None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"No such run: {run_id}")
        begin_stage(run, stage)


def begin_stage(run: Run, stage: Stage) -> None:
    """Mark a stage as started, against a caller's session.

    Split from the job wrapper the way ``services/scoring`` splits
    ``run_normalise_score`` from ``score_run``: the wrapper owns the session, the
    body takes one. Tests then drive this against the ``leads_test`` database
    instead of the real one.
    """
    if run.status == RunStatus.CANCELLED:
        raise RunCancelled(str(run.id))
    run.status = RunStatus.RUNNING
    if run.started_at is None:
        run.started_at = datetime.now(UTC)
    stats = dict(run.stats or {})
    stats["current_stage"] = stage.value
    run.stats = stats


def _finish(
    run_id: uuid.UUID,
    stage: Stage,
    *,
    elapsed: float,
    result: StageResult | None = None,
    unimplemented: str | None = None,
) -> None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        finish_stage(
            session,
            run,
            stage,
            elapsed=elapsed,
            result=result,
            unimplemented=unimplemented,
        )


def finish_stage(
    session,
    run: Run,
    stage: Stage,
    *,
    elapsed: float,
    result: StageResult | None = None,
    unimplemented: str | None = None,
) -> None:
    stats = dict(run.stats or {})

    if result is not None:
        # The stage body already wrote its own report under this key; merge
        # rather than replace, so the counters Phases 2–4 produce survive and
        # only the timing is added.
        existing = dict(stats.get(STATS_KEY[stage]) or {})
        existing.setdefault("processed", result.processed)
        existing.setdefault("produced", result.produced)
        # Recorded from here on, because §13 Screen 1's runtime estimate had
        # nothing to measure against — the six runs in the database carry only a
        # run-level wall clock that includes every re-run of every stage.
        existing["elapsed_seconds"] = round(elapsed, 1)
        stats[STATS_KEY[stage]] = existing

    if unimplemented:
        stats.setdefault("unimplemented", {})[stage.value] = unimplemented

    stats.pop("current_stage", None)
    completed = list(stats.get("stages_completed") or [])
    if stage.value not in completed:
        completed.append(stage.value)
    stats["stages_completed"] = completed
    run.stats = stats
    persist_source_state(session, run)


def _fail(run_id: uuid.UUID, stage: Stage, exc: Exception, *, elapsed: float) -> None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        fail_stage(run, stage, exc, elapsed=elapsed)
    log.error("stage.failed", run_id=str(run_id), stage=stage.value, error=str(exc))


def fail_stage(run: Run, stage: Stage, exc: Exception, *, elapsed: float) -> None:
    """§5.5 at the stage boundary: a run that could not do its work is not done."""
    run.status = RunStatus.FAILED
    run.error = f"{stage.value}: {type(exc).__name__}: {exc}"
    run.finished_at = datetime.now(UTC)
    stats = dict(run.stats or {})
    stats.pop("current_stage", None)
    stats.setdefault("failures", {})[stage.value] = {
        "error": str(exc)[:500],
        "elapsed_seconds": round(elapsed, 1),
    }
    run.stats = stats


def _enqueue_next(run_id: uuid.UUID, completed: Stage) -> None:
    """Hand the run to the next stage, or close it out."""
    from leadscraper.pipeline.queues import enqueue_stage

    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None or run.status == RunStatus.CANCELLED:
            return
        remaining = [
            s
            for s in planned_stages(run.sources_enabled or {})
            if STAGE_ORDER.index(s) > STAGE_ORDER.index(completed)
        ]
        if remaining:
            next_stage = remaining[0]
        else:
            close_run(run)
            return

    enqueue_stage(run_id, next_stage)


def close_run(run: Run) -> None:
    """Final status. ``partial`` is not a lesser ``done`` — it is a different fact.

    A run that skipped a stage the operator asked for, or hit one whose phase has
    not landed, must not report ``done``: §5.5's failure mode is harvesting
    nothing and not noticing, and a green "done" on an incomplete run is exactly
    how that happens.
    """
    stats = dict(run.stats or {})
    run.finished_at = datetime.now(UTC)
    run.status = RunStatus.PARTIAL if stats.get("unimplemented") else RunStatus.DONE
    stats.pop("current_stage", None)
    run.stats = stats


# --------------------------------------------------------------------------- #
# §7 breaker state → §13 Screen 2's pills
# --------------------------------------------------------------------------- #


def persist_source_state(session, run: Run) -> None:
    """Write per-source health where the API can read it.

    ``BreakerRegistry`` is in-process and dies with the worker, so the pills §13
    Screen 2 specifies had a table and no writer — ``source_state`` has been
    empty since Phase 1. The stage reports already carry the facts the breaker
    acted on, so they are projected here rather than reaching into another
    process's memory.
    """
    stats = run.stats or {}
    for stage, key in STATS_KEY.items():
        report = stats.get(key)
        if not isinstance(report, dict):
            continue
        source = _source_for(stage)
        if source is None:
            continue
        status, failures, error = _health(report)
        _upsert_source_state(session, run, source, status, failures, error)


def _source_for(stage: Stage) -> str | None:
    match stage:
        case Stage.DISCOVERY:
            return "google_maps"
        case Stage.CONTACT_ENRICHMENT:
            return "business_website"
        case Stage.SOCIAL_ENRICHMENT:
            return "facebook"
        case _:
            # Stages 5 and 6 are pure database work with no egress, so they have
            # no source health to report. A green pill for them would be noise.
            return None


def _health(report: dict) -> tuple[SourceStatus, int, str | None]:
    blocked = int(
        report.get("queries_blocked")
        or report.get("sites_blocked")
        or report.get("profiles_blocked")
        or 0
    )
    failed = int(
        report.get("queries_failed")
        or report.get("sites_failed")
        or report.get("profiles_failed")
        or 0
    )
    refused = int(report.get("sites_refused") or 0)

    if blocked:
        return SourceStatus.BLOCKED, blocked, f"{blocked} blocked"
    if refused:
        # §5.2: one host refusing is not the source refusing. A refusal count is
        # worth showing and is emphatically not a blocked source — a live run
        # lost 19 healthy domains to that conflation.
        return SourceStatus.THROTTLED, refused, f"{refused} host(s) refused"
    if failed:
        return SourceStatus.THROTTLED, failed, f"{failed} failed"
    return SourceStatus.OK, 0, None


def _upsert_source_state(
    session,
    run: Run,
    source: str,
    status: SourceStatus,
    failures: int,
    error: str | None,
) -> None:
    existing = session.execute(
        select(SourceState).where(
            SourceState.run_id == run.id, SourceState.source == source
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = SourceState(run_id=run.id, source=source)
        session.add(existing)
    existing.status = status
    existing.consecutive_failures = failures
    existing.last_error = error
    existing.updated_at = datetime.now(UTC)

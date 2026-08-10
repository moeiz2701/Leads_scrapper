"""§13 Screens 1 and 2 — create a run, watch it, cancel it, re-run one stage.

Two things this router refuses to do, both of them §5.5's failure mode wearing a
different hat:

* **It will not accept a run it cannot perform.** Asking for Facebook enables a
  stage whose body is Phase 8, so the request comes back naming the phase instead
  of starting a run that quietly omits a source the operator chose.
* **It will not start a Maps run without the §7.1 proxy gate satisfied.** Maps
  geo-ranks results, so a US egress IP answers a Lahore query with the wrong
  businesses — a full run of plausible, wrong data. ``resolve_proxy`` raises at
  stage 1 regardless; catching it here turns a failed run into a refused one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from leadscraper.api.deps import SessionDep
from leadscraper.api.schemas import (
    PreferenceUpdate,
    RunCreate,
    RunCreated,
    RunDetail,
    RunSummary,
    SourcePill,
    StageProgress,
)
from leadscraper.core.proxy import ProxyNotConfiguredError, resolve_proxy
from leadscraper.db.models import Business, Run, SourceState
from leadscraper.enums import RunMode, RunStatus, Stage
from leadscraper.pipeline.jobs import STATS_KEY, planned_stages, unavailable_stages
from leadscraper.pipeline.queues import (
    cancel_queued_jobs,
    enqueue_run,
    enqueue_stage,
    queue_depths,
)
from leadscraper.taxonomy import build_query_plan, get_city

router = APIRouter(prefix="/api/runs", tags=["runs"])

_TERMINAL = {RunStatus.DONE, RunStatus.FAILED, RunStatus.PARTIAL, RunStatus.CANCELLED}


@router.post("", response_model=RunCreated, status_code=status.HTTP_201_CREATED)
def create_run(payload: RunCreate, session: SessionDep) -> RunCreated:
    try:
        get_city(payload.city)
    except KeyError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    sources = payload.sources.model_dump()

    missing = unavailable_stages(sources)
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "A source you enabled has no implementation yet.",
                "stages": [s.value for s in missing],
                # Named rather than generic: "Phase 8" tells the operator to wait
                # for a release; "not implemented" reads like a bug to file.
                "message": (
                    "Facebook and Instagram enter at Stage 3 (social enrichment), "
                    "which implementation.md §16 schedules for Phase 8. Turn them "
                    "off to start a Maps + website run."
                ),
            },
        )

    warnings: list[str] = []
    if sources.get("google_maps", True):
        try:
            resolve_proxy("google_maps")
        except ProxyNotConfiguredError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "Google Maps needs a PK egress route (§7.1).",
                    "message": (
                        f"{exc} Maps geo-ranks results, so a non-PK IP answers a "
                        "Pakistani query with the wrong businesses — a full run of "
                        "plausible, wrong data. Set PROXY_URL, or opt out "
                        'explicitly with PROXY_REQUIRED_SOURCES="".'
                    ),
                },
            ) from exc

    if sources.get("directories"):
        # §16 Phase 6. Accepted rather than refused: directories are additive to a
        # Maps run, so the run is still the run the operator asked for, minus a
        # source that does not exist yet. Said out loud all the same.
        warnings.append(
            "Directory modules (§5.3) are Phase 6 and will not contribute to this run."
        )

    run = Run(
        mode=RunMode.DISCOVERY,
        city=payload.city,
        category=payload.category.value,
        subcategories=payload.subcategories or None,
        number_pref=payload.number_preference.value,
        sources_enabled=sources,
        target_leads=payload.target_leads,
        status=RunStatus.QUEUED,
        stats={
            "plan": {
                "queries": len(
                    build_query_plan(
                        payload.city,
                        payload.category,
                        payload.synonym_limit,
                        payload.tile_limit,
                    )
                ),
                "synonym_limit": payload.synonym_limit,
                "tile_limit": payload.tile_limit,
            }
        },
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    stages = planned_stages(sources)
    job_id = enqueue_run(run.id, stages)

    return RunCreated(
        run=_summary(session, run),
        stages_planned=[s.value for s in stages],
        stages_unavailable=[s.value for s in unavailable_stages(sources)],
        job_id=job_id,
        warnings=warnings,
    )


@router.get("", response_model=list[RunSummary])
def list_runs(session: SessionDep, limit: int = 50) -> list[RunSummary]:
    runs = session.execute(
        select(Run).order_by(Run.created_at.desc()).limit(limit)
    ).scalars()
    return [_summary(session, run) for run in runs]


@router.get("/{run_id}", response_model=RunDetail)
def get_run(run_id: uuid.UUID, session: SessionDep) -> RunDetail:
    run = _require(session, run_id)
    summary = _summary(session, run)
    stats = run.stats or {}

    return RunDetail(
        **summary.model_dump(),
        subcategories=list(run.subcategories or []),
        sources_enabled=run.sources_enabled or {},
        stats=stats,
        stages=_stage_progress(run),
        sources=_source_pills(session, run),
        queue_depths=queue_depths(),
        unattributed_ceiling=(stats.get("normalise_score") or {}).get(
            "unattributed_ceiling"
        ),
    )


@router.post("/{run_id}/cancel", response_model=RunDetail)
def cancel_run(run_id: uuid.UUID, session: SessionDep) -> RunDetail:
    """§13 Screen 2's Cancel button.

    Drops every queued stage and marks the run cancelled, which stops the chain
    at the next stage boundary. A stage already executing finishes — see
    ``queues.cancel_queued_jobs`` for why, and the UI says so rather than
    implying an instant stop.
    """
    run = _require(session, run_id)
    if run.status in _TERMINAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Run is already {run.status} and cannot be cancelled.",
        )

    cancelled = cancel_queued_jobs(run_id)
    run.status = RunStatus.CANCELLED
    run.finished_at = datetime.now(UTC)
    stats = dict(run.stats or {})
    stats["cancelled_jobs"] = cancelled
    run.stats = stats
    session.commit()
    return get_run(run_id, session)


@router.post("/{run_id}/stages/{stage}", response_model=RunDetail)
def rerun_stage(run_id: uuid.UUID, stage: Stage, session: SessionDep) -> RunDetail:
    """§2's whole reason for having queues: re-run one stage, not the pipeline.

    Runs exactly this stage and stops — it does not chain, because an operator
    re-running enrichment after fixing a selector does not want discovery
    re-scraped behind it.
    """
    run = _require(session, run_id)
    from leadscraper.pipeline.stages import IMPLEMENTED_STAGES

    if stage not in IMPLEMENTED_STAGES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Stage {stage.value!r} has no implementation yet (see §16).",
        )

    if run.status == RunStatus.CANCELLED:
        # Re-running a stage is an explicit instruction, so it un-cancels rather
        # than silently doing nothing — the alternative is a button that appears
        # to work and does not.
        run.status = RunStatus.QUEUED
        session.commit()

    enqueue_stage(run_id, stage, chain=False)
    return get_run(run_id, session)


@router.patch("/{run_id}/preference", response_model=RunDetail)
def set_preference(
    run_id: uuid.UUID, payload: PreferenceUpdate, session: SessionDep
) -> RunDetail:
    """Change §3.3's ``number_preference`` and re-rank.

    **Ranking stays in Stage 5 rather than moving into the exporter.** Both are
    defensible and this is the reason for the choice: ``contacts.rank`` is a real
    indexed column that §12.1, §13 Screen 3 and the CSV all read, so it must
    never disagree with the run's preference. One writer keeps that true. The
    stage is pure database work, idempotent, and takes about a second on the
    199-business run — cheap enough that correctness wins outright.
    """
    run = _require(session, run_id)
    run.number_pref = payload.number_preference.value
    session.commit()

    enqueue_stage(run_id, Stage.NORMALISE_SCORE, chain=False)
    return get_run(run_id, session)


# --------------------------------------------------------------------------- #
# Projection
# --------------------------------------------------------------------------- #


def _require(session, run_id: uuid.UUID) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such run: {run_id}")
    return run


def _summary(session, run: Run) -> RunSummary:
    businesses = (
        session.scalar(select(func.count(Business.id)).where(Business.run_id == run.id))
        or 0
    )
    scoring = (run.stats or {}).get("normalise_score") or {}
    return RunSummary(
        id=run.id,
        city=run.city,
        category=run.category,
        number_preference=run.number_pref,
        status=run.status,
        mode=run.mode,
        businesses=businesses,
        qualified=int(scoring.get("qualified") or 0),
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
    )


def _stage_progress(run: Run) -> list[StageProgress]:
    """§13 Screen 2's per-stage counters, read from ``runs.stats``.

    Every stage already writes its report there under its own key, so this
    projects that rather than maintaining a second counter path that could
    disagree with the reports the run summary prints.
    """
    stats = run.stats or {}
    completed = set(stats.get("stages_completed") or [])
    current = stats.get("current_stage")
    unimplemented = stats.get("unimplemented") or {}
    failures = stats.get("failures") or {}
    planned = set(planned_stages(run.sources_enabled or {}))

    rows: list[StageProgress] = []
    for stage in Stage:
        report = stats.get(STATS_KEY[stage]) or {}
        if stage.value in failures:
            state = "failed"
        elif stage.value in unimplemented:
            state = "unimplemented"
        elif current == stage.value:
            state = "running"
        elif stage.value in completed or report:
            # `report` alone counts a stage as done: the six runs already in the
            # database predate `stages_completed` and would otherwise show an
            # empty Screen 2 despite having full stats.
            state = "done"
        elif stage in planned:
            state = "pending"
        else:
            state = "skipped"

        rows.append(
            StageProgress(
                stage=stage.value,
                state=state,
                processed=report.get("processed") or report.get("businesses_total"),
                produced=report.get("produced") or report.get("created"),
                elapsed_seconds=report.get("elapsed_seconds"),
                detail={k: v for k, v in report.items() if not isinstance(v, dict)},
                note=unimplemented.get(stage.value) or (failures.get(stage.value) or {}).get(
                    "error"
                ),
            )
        )
    return rows


def _source_pills(session, run: Run) -> list[SourcePill]:
    states = session.execute(
        select(SourceState).where(SourceState.run_id == run.id)
    ).scalars()
    return [
        SourcePill(
            source=state.source,
            status=state.status,
            detail=state.last_error,
            updated_at=state.updated_at,
        )
        for state in states
    ]

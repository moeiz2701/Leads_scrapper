"""Stage 1 orchestration: fan out over Maps queries and ingest what comes back.

Keeps the stage function in ``pipeline/stages.py`` thin, so the interesting part
— query planning, ingest, and run bookkeeping — is testable without RQ.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from leadscraper.config import get_settings
from leadscraper.core.cache import FetchCache
from leadscraper.core.proxy import ProxyNotConfiguredError
from leadscraper.db.models import Run
from leadscraper.db.session import session_scope
from leadscraper.enums import RunStatus, Stage
from leadscraper.logging import get_logger
from leadscraper.pipeline.stages import StageResult
from leadscraper.services.ingest import IngestStats, ingest_maps_result
from leadscraper.sources.google_maps import GoogleMapsSource, QueryOutcome
from leadscraper.taxonomy import QuerySpec, build_query_plan

log = get_logger(__name__)


@dataclass(slots=True)
class DiscoveryReport:
    queries_planned: int = 0
    queries_run: int = 0
    queries_from_cache: int = 0
    queries_blocked: int = 0
    queries_failed: int = 0
    queries_empty: int = 0
    raw_results: int = 0
    ingest: IngestStats | None = None

    def as_dict(self) -> dict:
        data = {
            "queries_planned": self.queries_planned,
            "queries_run": self.queries_run,
            "queries_from_cache": self.queries_from_cache,
            "queries_blocked": self.queries_blocked,
            "queries_failed": self.queries_failed,
            "queries_empty": self.queries_empty,
            "raw_results": self.raw_results,
        }
        if self.ingest:
            data.update(self.ingest.as_dict())
        return data


def run_maps_discovery(
    run_id: uuid.UUID,
    synonym_limit: int | None = None,
    tile_limit: int | None = None,
) -> StageResult:
    """Plan queries for the run, execute them, and ingest the results."""
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"No such run: {run_id}")
        if not run.city or not run.category:
            raise ValueError(
                f"Run {run_id} has no city/category — seed-mode runs (§3.2) skip "
                f"Stage 1 and enter at Stage 2."
            )

        plan = build_query_plan(run.city, run.category, synonym_limit, tile_limit)
        run.status = RunStatus.RUNNING
        run.started_at = run.started_at or datetime.now(UTC)
        session.flush()

        report = _execute(session, run, plan)
        run.stats = {**(run.stats or {}), "discovery": report.as_dict()}
        session.flush()

    stats = report.ingest or IngestStats()
    return StageResult(
        stage=Stage.DISCOVERY,
        run_id=run_id,
        processed=report.queries_planned,
        produced=stats.created,
        skipped=report.queries_from_cache,
        failed=report.queries_blocked + report.queries_failed,
        notes={k: str(v) for k, v in report.as_dict().items()},
    )


def _execute(session: Session, run: Run, plan: list[QuerySpec]) -> DiscoveryReport:
    report = DiscoveryReport(queries_planned=len(plan))
    ingest_stats = IngestStats()
    report.ingest = ingest_stats

    settings = get_settings()
    cache = FetchCache(session=session, settings=settings)

    try:
        source = GoogleMapsSource(cache=cache, settings=settings)
    except ProxyNotConfiguredError as exc:
        # §7.1 — refuse rather than return geo-ranked-for-the-wrong-country data.
        # Surfaced as a failed run with the reason, not a silent empty result.
        log.error("maps.proxy_missing", run_id=str(run.id), error=str(exc))
        run.status = RunStatus.FAILED
        run.error = str(exc)
        run.finished_at = datetime.now(UTC)
        return report

    by_query = {spec.query: spec for spec in plan}
    outcomes: list[QueryOutcome] = asyncio.run(source.discover([s.query for s in plan]))

    for outcome in outcomes:
        spec = by_query.get(outcome.query)
        if outcome.from_cache:
            report.queries_from_cache += 1
        elif outcome.blocked:
            report.queries_blocked += 1
            continue
        elif outcome.error:
            report.queries_failed += 1
            continue
        else:
            report.queries_run += 1

        if not outcome.results:
            report.queries_empty += 1
            continue

        report.raw_results += len(outcome.results)
        for result in outcome.results:
            ingest_maps_result(
                session,
                run.id,
                result,
                area=spec.tile if spec else None,
                city=run.city,
                category=run.category,
                stats=ingest_stats,
            )

    _finalise(run, report)
    return report


def _finalise(run: Run, report: DiscoveryReport) -> None:
    """Mark the run's outcome honestly.

    A run where most queries were blocked is ``partial``, not ``done`` — §13
    Screen 2 shows this, and calling a half-blocked run "done" is how you end up
    trusting a thin table.
    """
    attempted = report.queries_run + report.queries_from_cache
    degraded = report.queries_blocked + report.queries_failed

    if attempted == 0 and degraded:
        run.status = RunStatus.FAILED
        run.error = "every query was blocked or failed"
    elif degraded:
        run.status = RunStatus.PARTIAL
    else:
        run.status = RunStatus.DONE
    run.finished_at = datetime.now(UTC)

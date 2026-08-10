"""RQ wiring — one queue per stage (§2).

§2 is emphatic that this must not be one browser script: selectors break weekly
and you need to re-run stage 3 without re-running stage 1. Separate queues are
what make that possible — each stage's backlog, failures and retries are visible
and drainable on their own.

Phase 5 adds the half that was missing: producers, a cancel path, and a worker
entrypoint (``scripts/worker.py``). See ``pipeline/jobs.py`` for what a worker
actually executes and why the stages chain themselves.
"""

from __future__ import annotations

import functools
import uuid
from typing import Any

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue
from rq.job import Job

from leadscraper.config import get_settings
from leadscraper.enums import Stage
from leadscraper.logging import get_logger

log = get_logger(__name__)

# Browser stages are slow; plain-fetch stages are not. Timeouts are per stage so
# a stuck Maps panel does not set the timeout policy for the whole system.
STAGE_TIMEOUTS: dict[Stage, int] = {
    Stage.DISCOVERY: 60 * 60,
    Stage.CONTACT_ENRICHMENT: 60 * 45,
    Stage.SOCIAL_ENRICHMENT: 60 * 30,
    Stage.PERSON_ATTRIBUTION: 60 * 20,
    Stage.NORMALISE_SCORE: 60 * 15,
    Stage.DEDUPE_EXPORT: 60 * 15,
}

# §5.5: "One stubborn record must never stall a queue." Retries are cheap; the
# reveal-failed path in §5.5 handles the rest on the next run.
STAGE_MAX_RETRIES = 2

# Finished jobs stay inspectable for a day — long enough for the §13 Screen 2 log
# tail to explain a run the operator walked away from.
RESULT_TTL = 60 * 60 * 24

JOB_FUNCTION = "leadscraper.pipeline.jobs.run_stage_job"


@functools.lru_cache(maxsize=1)
def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url)


@functools.cache
def get_queue(stage: Stage) -> Queue:
    """The queue for one stage.

    ``queue_sync`` makes every queue inline (RQ's ``is_async=False``), which runs
    the job inside ``enqueue`` and — because jobs chain themselves — runs the
    whole pipeline inside the caller. That is the synchronous mode, reached by
    one setting rather than by a second code path, so the two cannot drift.
    """
    return Queue(
        name=f"stage.{stage.value}",
        connection=get_redis(),
        default_timeout=STAGE_TIMEOUTS[stage],
        is_async=not get_settings().queue_sync,
    )


def all_queues() -> list[Queue]:
    return [get_queue(stage) for stage in Stage]


def queue_depths() -> dict[str, int]:
    """Per-stage backlog, for the §13 Screen 2 counters."""
    try:
        return {stage.value: len(get_queue(stage)) for stage in Stage}
    except RedisError as exc:
        # The UI polls this. A dead Redis must show as unknown rather than as a
        # confident row of zeros — "nothing queued" and "cannot tell" are
        # different facts and only one of them means the run is progressing.
        log.warning("queues.depth_unavailable", error=str(exc))
        return {}


def enqueue_stage(run_id: uuid.UUID | str, stage: Stage, *, chain: bool = True) -> str:
    """Put one stage on its queue. Returns the RQ job id.

    ``chain=False`` runs exactly this stage and stops — which is §2's "re-run
    stage 3 without re-running stage 1", and how the API re-scores a run after
    the operator changes ``number_preference``.
    """
    job = get_queue(stage).enqueue(
        JOB_FUNCTION,
        str(run_id),
        stage.value,
        chain=chain,
        retry=None,
        result_ttl=RESULT_TTL,
        failure_ttl=RESULT_TTL,
        meta={"run_id": str(run_id), "stage": stage.value},
        description=f"{stage.value} · run {str(run_id)[:8]}",
    )
    log.info("queue.enqueued", run_id=str(run_id), stage=stage.value, job_id=job.id)
    return job.id


def enqueue_run(run_id: uuid.UUID | str, stages: list[Stage]) -> str | None:
    """Start a run at its first stage; the rest chain themselves.

    Only the head is enqueued on purpose. Enqueuing all six up front would put
    jobs in the queue for stages that a failure upstream has already made
    pointless, and §5.5's rule is that work which cannot succeed must not look
    pending.
    """
    if not stages:
        return None
    return enqueue_stage(run_id, stages[0])


def jobs_for_run(run_id: uuid.UUID | str) -> list[Job]:
    """Every queued job belonging to a run, across all stage queues."""
    wanted = str(run_id)
    found: list[Job] = []
    for queue in all_queues():
        for job in queue.jobs:
            if (job.meta or {}).get("run_id") == wanted:
                found.append(job)
    return found


def cancel_queued_jobs(run_id: uuid.UUID | str) -> int:
    """§13 Screen 2's Cancel button, on the queue side.

    **What Cancel actually does**, stated plainly because the UI must not imply
    more: it drops every *queued* stage immediately, and the run is marked
    cancelled so the next stage refuses to start (``jobs._begin``). A stage
    already executing runs to completion — killing a worker mid-Playwright would
    leave the §7 cache and the breaker state inconsistent, and the longest stage
    here is minutes, not hours. Since stages chain themselves there is at most
    one job in flight per run, so "the current stage, then stop" is the whole
    behaviour.
    """
    cancelled = 0
    for job in jobs_for_run(run_id):
        try:
            job.cancel()
            cancelled += 1
        except Exception as exc:  # noqa: BLE001 — a job that finished mid-scan
            log.debug("queue.cancel_noop", job_id=job.id, error=str(exc))
    return cancelled


def queue_health() -> dict[str, Any]:
    """Is there anything to consume these queues? For the §13 Settings screen."""
    from rq import Worker

    try:
        workers = Worker.all(connection=get_redis())
        return {
            "redis": True,
            "workers": len(workers),
            "sync_mode": get_settings().queue_sync,
            "depths": queue_depths(),
        }
    except RedisError as exc:
        # Worth surfacing loudly: with no worker and no sync mode, a created run
        # sits at `queued` forever and looks like a hang rather than a
        # misconfiguration.
        return {"redis": False, "workers": 0, "error": str(exc), "depths": {}}

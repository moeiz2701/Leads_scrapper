"""RQ wiring — one queue per stage (§2).

§2 is emphatic that this must not be one browser script: selectors break weekly
and you need to re-run stage 3 without re-running stage 1. Separate queues are
what make that possible — each stage's backlog, failures and retries are visible
and drainable on their own.
"""

from __future__ import annotations

import functools

from redis import Redis
from rq import Queue

from leadscraper.config import get_settings
from leadscraper.enums import Stage

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


@functools.lru_cache(maxsize=1)
def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url)


@functools.cache
def get_queue(stage: Stage) -> Queue:
    return Queue(
        name=f"stage.{stage.value}",
        connection=get_redis(),
        default_timeout=STAGE_TIMEOUTS[stage],
    )


def all_queues() -> list[Queue]:
    return [get_queue(stage) for stage in Stage]


def queue_depths() -> dict[str, int]:
    """Per-stage backlog, for the §13 Screen 2 counters."""
    return {stage.value: len(get_queue(stage)) for stage in Stage}

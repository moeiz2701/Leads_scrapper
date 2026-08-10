"""The queue consumer. §2's stages have had producers since Phase 5; this eats them.

    uv run python scripts/worker.py                 # every stage
    uv run python scripts/worker.py --stage discovery
    uv run python scripts/worker.py --burst         # drain and exit

**``SimpleWorker``, not ``Worker``.** RQ's default worker forks a child per job,
and ``os.fork`` does not exist on Windows — the default worker dies on start
here. ``SimpleWorker`` executes in the worker process instead. The cost is real
and worth stating: a job that segfaults or is killed takes the worker with it,
where a forking worker would have survived and marked the job failed. At this
scale that is the right trade, and ``jobs.run_stage_job`` records the failure on
the run before re-raising, so the record survives even when the process does not.

Run one worker per stage if you want a stuck Maps run to stop blocking scoring;
one worker for everything is fine for a single operator, which is what §13
describes.
"""

from __future__ import annotations

import argparse
import os
import sys

from rq import SimpleWorker, Worker

from leadscraper.enums import Stage
from leadscraper.logging import configure_logging, get_logger
from leadscraper.pipeline.queues import all_queues, get_queue, get_redis

log = get_logger(__name__)


def _worker_class() -> type[Worker]:
    """Fork where we can, run in-process where we cannot."""
    if hasattr(os, "fork"):
        return Worker
    return SimpleWorker


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        action="append",
        choices=[s.value for s in Stage],
        help="consume only these stage queues (repeatable); default is all six",
    )
    parser.add_argument(
        "--burst",
        action="store_true",
        help="process what is queued now, then exit — for scripted runs and CI",
    )
    args = parser.parse_args()

    configure_logging()

    queues = (
        [get_queue(Stage(name)) for name in args.stage] if args.stage else all_queues()
    )

    worker_class = _worker_class()
    log.info(
        "worker.starting",
        worker_class=worker_class.__name__,
        queues=[q.name for q in queues],
        burst=args.burst,
    )
    print(
        f"Consuming {len(queues)} queue(s) as {worker_class.__name__}: "
        + ", ".join(q.name for q in queues)
    )

    worker = worker_class(queues, connection=get_redis())
    worker.work(burst=args.burst, with_scheduler=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

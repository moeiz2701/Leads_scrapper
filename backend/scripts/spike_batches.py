"""Measure the batch cascade against every run in the database.

Read-only: no writes, no network. Prints each run's split so _BATCH_SPEC.md's
counts can be checked against real data rather than trusted, and so the
food-only scope is visible as a number rather than as a claim.

    PYTHONIOENCODING=utf-8 uv run python scripts/spike_batches.py
"""

from __future__ import annotations

from sqlalchemy import select

from leadscraper.core import batches
from leadscraper.db.models import Run
from leadscraper.db.session import get_session
from leadscraper.services.results import ResultQuery, fetch_results


def main() -> None:
    session = get_session()
    try:
        runs = session.execute(select(Run).order_by(Run.created_at)).scalars().all()
        for run in runs:
            page = fetch_results(session, ResultQuery(run_ids=(run.id,)))
            counts = {k: v for k, v in page.batch_counts.items() if v}
            print(f"\n{run.city} x {run.category} [{run.status}] — {page.total} rows")
            if not counts:
                print("  (no visible rows)")
                continue
            for slug, count in sorted(counts.items(), key=lambda kv: -kv[1]):
                spec = batches.BY_SLUG.get(slug)
                label = f"{spec.id} {spec.name}" if spec else "-- no batch defined"
                share = 100 * count / page.total
                print(f"  {label:<28} {count:>4}  {share:5.1f}%")
            assert sum(page.batch_counts.values()) == page.total, "not exhaustive"

            sendable = sum(
                count
                for slug, count in page.batch_counts.items()
                if (spec := batches.BY_SLUG.get(slug)) and spec.sendable
            )
            print(f"  {'sendable':<28} {sendable:>4}")
    finally:
        session.close()


if __name__ == "__main__":
    main()

"""Run the §5.3 directory corroboration pass over an existing run.

The second input to Stage 2. Where ``run_enrichment.py`` crawls each business's
own site to *prove* a number, this joins BusinessList.pk's listing of the same
city × category onto the businesses the run already has — a second opinion, which
is the one thing §10.2's ``source_agreement`` term has never had on a run without
websites.

    uv run python scripts/run_directories.py --run-id <uuid>
    uv run python scripts/run_directories.py --latest
    uv run python scripts/run_directories.py --latest --insert-unmatched

Pure re-run safety: the §7 cache means a second pass inside the 7-day listing TTL
makes no requests, and the merge is gap-fill only, so running it twice changes
nothing the first pass did not already do.
"""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import func, select

from leadscraper.db.models import Business, Contact, Run
from leadscraper.db.session import session_scope
from leadscraper.enums import ContactKind, LineType, Source
from leadscraper.logging import configure_logging
from leadscraper.services.directories import run_directory_corroboration


def _resolve_run_id(args: argparse.Namespace) -> uuid.UUID | None:
    if args.run_id:
        return uuid.UUID(args.run_id)
    with session_scope() as session:
        run = session.execute(
            select(Run).order_by(Run.created_at.desc()).limit(1)
        ).scalar_one_or_none()
        return run.id if run else None


def _counts(run_id: uuid.UUID) -> dict[str, int]:
    with session_scope() as session:

        def phone_count(*where) -> int:
            return (
                session.scalar(
                    select(func.count(Contact.id))
                    .select_from(Contact)
                    .join(Business)
                    .where(
                        Business.run_id == run_id,
                        Contact.kind == ContactKind.PHONE,
                        *where,
                    )
                )
                or 0
            )

        # §10.2's source_agreement term, counted the way the scorer counts it:
        # per business, over distinct contact sources.
        multi_source = (
            session.scalar(
                select(func.count())
                .select_from(
                    select(Contact.business_id)
                    .join(Business)
                    .where(Business.run_id == run_id)
                    .group_by(Contact.business_id)
                    .having(func.count(func.distinct(Contact.source)) >= 2)
                    .subquery()
                )
            )
            or 0
        )

        return {
            "businesses": session.scalar(
                select(func.count(Business.id)).where(Business.run_id == run_id)
            )
            or 0,
            "phone contacts": phone_count(),
            "mobiles": phone_count(Contact.line_type == LineType.MOBILE),
            "from businesslist": phone_count(Contact.source == Source.BUSINESSLIST_PK),
            "businesses w/ 2+ sources": multi_source,
            "with lat/lng": session.scalar(
                select(func.count(Business.id)).where(
                    Business.run_id == run_id, Business.lat.isnot(None)
                )
            )
            or 0,
            "with review_count": session.scalar(
                select(func.count(Business.id)).where(
                    Business.run_id == run_id, Business.review_count.isnot(None)
                )
            )
            or 0,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", help="run UUID to corroborate")
    parser.add_argument("--latest", action="store_true", help="use the most recent run")
    parser.add_argument(
        "--insert-unmatched",
        action="store_true",
        help="keep directory rows that matched no business as new businesses (§5.3)",
    )
    args = parser.parse_args()

    if not args.run_id and not args.latest:
        parser.error("pass --run-id <uuid> or --latest")

    configure_logging()

    run_id = _resolve_run_id(args)
    if run_id is None:
        print("No runs found. Run scripts/run_discovery.py first.")
        return 1

    with session_scope() as session:
        run = session.get(Run, run_id)
        slice_name = f"{run.city} x {run.category}" if run else "?"

    before = _counts(run_id)
    print(f"Run {run_id}  ({slice_name})\n")
    print("Before:")
    for key, value in before.items():
        print(f"  {key:26} {value}")

    result = run_directory_corroboration(
        run_id, insert_unmatched=True if args.insert_unmatched else None
    )

    after = _counts(run_id)
    with session_scope() as session:
        run = session.get(Run, run_id)
        stats = (run.stats or {}).get("directories", {}) if run else {}
        status = run.status if run else "?"

    print("\n" + "=" * 66)
    print(f"Stage 2 - directory corroboration (§5.3)   status={status}")
    print("=" * 66)
    for key, value in stats.items():
        print(f"  {key:26} {value}")
    print("-" * 66)
    for key, value in after.items():
        delta = value - before[key]
        arrow = f"  (+{delta})" if delta else ""
        print(f"  {key:26} {value}{arrow}")
    print("=" * 66)
    print(f"\nStageResult: {result.as_dict()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

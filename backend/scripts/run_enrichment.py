"""Run Stage 2 website enrichment over an existing run, from the command line.

Stage 1 (``run_discovery.py``) leaves you a table of businesses whose WhatsApp
column is entirely §9.3 inference. This is the pass that confirms it.

    uv run python scripts/run_enrichment.py --run-id <uuid>
    uv run python scripts/run_enrichment.py --latest --limit 40

``--limit`` caps how many domains get crawled, which is what you want for a
smoke run: a full Lahore × salon run is a few hundred domains and ~8 minutes.
"""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import func, select

from leadscraper.db.models import Business, Contact, Run
from leadscraper.db.session import session_scope
from leadscraper.enums import ContactKind, LineType, WhatsAppLabel
from leadscraper.logging import configure_logging
from leadscraper.services.enrichment import run_website_enrichment


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
            return session.scalar(
                select(func.count(Contact.id))
                .select_from(Contact)
                .join(Business)
                .where(Business.run_id == run_id, Contact.kind == ContactKind.PHONE, *where)
            ) or 0

        return {
            "businesses": session.scalar(
                select(func.count(Business.id)).where(Business.run_id == run_id)
            ) or 0,
            "with website": session.scalar(
                select(func.count(Business.id)).where(
                    Business.run_id == run_id, Business.website.isnot(None)
                )
            ) or 0,
            "phone contacts": phone_count(),
            "mobiles": phone_count(Contact.line_type == LineType.MOBILE),
            "whatsapp confirmed": phone_count(Contact.wa_label == WhatsAppLabel.CONFIRMED),
            "whatsapp likely": phone_count(Contact.wa_label == WhatsAppLabel.LIKELY),
            "emails": session.scalar(
                select(func.count(Contact.id))
                .select_from(Contact)
                .join(Business)
                .where(Business.run_id == run_id, Contact.kind == ContactKind.EMAIL)
            ) or 0,
            "with a person name": phone_count(Contact.person_name.isnot(None)),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", help="run UUID to enrich")
    parser.add_argument("--latest", action="store_true", help="use the most recent run")
    parser.add_argument("--limit", type=int, default=None, help="max domains to crawl")
    args = parser.parse_args()

    if not args.run_id and not args.latest:
        parser.error("pass --run-id <uuid> or --latest")

    configure_logging()

    run_id = _resolve_run_id(args)
    if run_id is None:
        print("No runs found. Run scripts/run_discovery.py first.")
        return 1

    before = _counts(run_id)
    print(f"Run {run_id}\n")
    print("Before:")
    for key, value in before.items():
        print(f"  {key:24} {value}")

    result = run_website_enrichment(run_id, limit=args.limit)

    after = _counts(run_id)
    with session_scope() as session:
        run = session.get(Run, run_id)
        stats = (run.stats or {}).get("website_enrichment", {}) if run else {}
        status = run.status if run else "?"

    print("\n" + "=" * 62)
    print(f"Stage 2 · website enrichment   status={status}")
    print("=" * 62)
    for key, value in stats.items():
        print(f"  {key:24} {value}")
    print("-" * 62)
    for key, value in after.items():
        delta = value - before[key]
        arrow = f"  (+{delta})" if delta else ""
        print(f"  {key:24} {value}{arrow}")
    print("=" * 62)
    print(f"\nStageResult: {result.as_dict()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

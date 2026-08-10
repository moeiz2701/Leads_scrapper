"""Run Stages 5 and 6 over an existing run, from the command line.

Stage 2 leaves you a table of businesses and contacts with no order to them.
This is the pass that turns it into a ranked lead list.

    uv run python scripts/run_scoring.py --latest
    uv run python scripts/run_scoring.py --run-id <uuid> --preference whatsapp_only
    uv run python scripts/run_scoring.py --all          # every run in the database

Pure database work — no network, no browser, no cache. Re-running is safe and
idempotent: the same inputs produce the same scores and the same ranks.
"""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import func, select

from leadscraper.db.models import Business, Contact, Run
from leadscraper.db.session import session_scope
from leadscraper.enums import ContactKind, NumberPreference, WhatsAppLabel
from leadscraper.logging import configure_logging
from leadscraper.services.dedupe import run_dedupe
from leadscraper.services.scoring import run_normalise_score

TOP_N = 15


def _resolve(args: argparse.Namespace) -> list[uuid.UUID]:
    with session_scope() as session:
        if args.run_id:
            return [uuid.UUID(args.run_id)]
        query = select(Run).order_by(Run.created_at.desc())
        if not args.all:
            query = query.limit(1)
        return [run.id for run in session.execute(query).scalars()]


def _headline(run_id: uuid.UUID) -> dict[str, object]:
    with session_scope() as session:
        run = session.get(Run, run_id)
        businesses = session.scalar(
            select(func.count(Business.id)).where(Business.run_id == run_id)
        ) or 0
        qualified = session.scalar(
            select(func.count(Business.id)).where(
                Business.run_id == run_id, Business.lead_score >= 60
            )
        ) or 0
        confirmed = session.scalar(
            select(func.count(func.distinct(Business.id)))
            .select_from(Business)
            .join(Contact)
            .where(
                Business.run_id == run_id,
                Contact.wa_label == WhatsAppLabel.CONFIRMED,
            )
        ) or 0
        return {
            "city": run.city if run else "?",
            "category": run.category if run else "?",
            "preference": run.number_pref if run else "?",
            "businesses": businesses,
            "score >= 60": qualified,
            "with a confirmed number": confirmed,
        }


def _top(run_id: uuid.UUID) -> list[tuple]:
    """The §13 Screen 3 default view: lead_score DESC, with phone_1."""
    with session_scope() as session:
        rows = []
        businesses = session.execute(
            select(Business)
            .where(Business.run_id == run_id)
            .order_by(Business.lead_score.desc().nullslast(), Business.name)
            .limit(TOP_N)
        ).scalars()
        for business in businesses:
            phones = sorted(
                (c for c in business.contacts
                 if c.kind == ContactKind.PHONE and c.rank is not None),
                key=lambda c: c.rank,
            )
            first = phones[0] if phones else None
            rows.append(
                (
                    business.lead_score,
                    business.name[:34],
                    first.value_e164 if first else "—",
                    (first.wa_label if first else "—") or "—",
                    (first.line_type if first else "—") or "—",
                    len(phones),
                    business.rating if business.rating is not None else "—",
                    business.review_count if business.review_count is not None else "—",
                )
            )
        return rows


def _report(title: str, stats: dict) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")
    for key, value in stats.items():
        print(f"  {key:30} {value}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", help="run UUID to score")
    parser.add_argument("--latest", action="store_true", help="use the most recent run")
    parser.add_argument("--all", action="store_true", help="score every run")
    parser.add_argument(
        "--preference",
        choices=[p.value for p in NumberPreference],
        help="override the run's §3.3 number_preference before ranking",
    )
    parser.add_argument("--skip-dedupe", action="store_true", help="Stage 5 only")
    args = parser.parse_args()

    if not (args.run_id or args.latest or args.all):
        parser.error("pass --run-id <uuid>, --latest or --all")

    configure_logging()

    run_ids = _resolve(args)
    if not run_ids:
        print("No runs found. Run scripts/run_discovery.py first.")
        return 1

    for run_id in run_ids:
        if args.preference:
            with session_scope() as session:
                run = session.get(Run, run_id)
                if run:
                    run.number_pref = args.preference

        before = _headline(run_id)
        print(f"\nRun {run_id}   {before['city']} × {before['category']}")

        scored = run_normalise_score(run_id)
        _report("Stage 5 · normalise, score (§10.2), rank (§3.3)", scored.notes)

        if not args.skip_dedupe:
            deduped = run_dedupe(run_id)
            _report("Stage 6 · dedupe (§10.1)", deduped.notes)

        _report("Run totals", _headline(run_id))

        print(f"\n  Top {TOP_N} by lead_score (§13 Screen 3 default sort)")
        print(f"  {'score':>5}  {'business':34}  {'phone_1':15} {'wa':9} "
              f"{'type':9} {'#':>2} {'rating':>6} {'revs':>6}")
        print("  " + "-" * 96)
        for row in _top(run_id):
            score, name, phone, wa, line_type, count, rating, reviews = row
            print(f"  {score:>5}  {name:34}  {phone:15} {wa:9} {line_type:9} "
                  f"{count:>2} {rating:>6} {reviews:>6}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

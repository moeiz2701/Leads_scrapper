"""Run the §6 social pass (Stage 3) over an existing run.

Renders each business's Facebook Page — then its Instagram profile, if the §6.6
per-business request cap allows a second — logged out, and folds what they
publish into the contacts table.

    uv run python scripts/run_social.py --run-id <uuid>
    uv run python scripts/run_social.py --latest
    uv run python scripts/run_social.py --latest --limit 25

**This stage is slow on purpose.** §6.6 sets 8–20s randomised delays at
concurrency 1, and §6.7 measured that the page has to be *rendered* — so budget
roughly 20 seconds per business and expect a 140-business slice to take half an
hour. The §7 cache holds rendered bodies for 30 days, so a second pass is mostly free;
check ``from_cache`` in the output before concluding anything about live
behaviour.

**A re-run is not entirely free, and that is deliberate.** About a third of
profiles come back as an application shell — HTTP 200 carrying no profile — and
those bodies are *not* cached (see ``sources/social.py``), because a 30-day TTL
on a non-result would convert one transient soft-gate into a month of permanent
misses. So each pass retries them: on Lahore × food that is ~40 requests,
roughly 13 minutes. Some of them render on the retry, which is the point.

The merge is upgrade-only and gap-fill only, so running it twice changes nothing
the first pass did not already do.
"""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import func, select

from leadscraper.db.models import Business, Contact, Run
from leadscraper.db.session import session_scope
from leadscraper.enums import ContactKind, LineType, Source, WhatsAppLabel
from leadscraper.logging import configure_logging
from leadscraper.services.social import run_social_enrichment


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

        def business_count(*where) -> int:
            return (
                session.scalar(
                    select(func.count(Business.id)).where(Business.run_id == run_id, *where)
                )
                or 0
            )

        # The number this stage exists to move. Counted per *business*, because
        # one business with three confirmed numbers is still one contactable
        # lead and §10.2 scores it once.
        businesses_confirmed = (
            session.scalar(
                select(func.count())
                .select_from(
                    select(Contact.business_id)
                    .join(Business)
                    .where(
                        Business.run_id == run_id,
                        Contact.wa_label == WhatsAppLabel.CONFIRMED,
                    )
                    .group_by(Contact.business_id)
                    .subquery()
                )
            )
            or 0
        )

        return {
            "businesses": business_count(),
            "with a facebook url": business_count(Business.facebook_url.isnot(None)),
            "with an instagram url": business_count(Business.instagram_url.isnot(None)),
            "with a website": business_count(Business.website.isnot(None)),
            "phone contacts": phone_count(),
            "mobiles": phone_count(Contact.line_type == LineType.MOBILE),
            "confirmed contacts": phone_count(Contact.wa_label == WhatsAppLabel.CONFIRMED),
            "businesses w/ confirmed": businesses_confirmed,
            "from facebook": phone_count(Contact.source == Source.FACEBOOK),
            "from instagram": phone_count(Contact.source == Source.INSTAGRAM),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", help="run UUID to enrich")
    parser.add_argument("--latest", action="store_true", help="use the most recent run")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap the number of businesses read (not profiles) — for a quick slice",
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
    print("\nRendering profiles — §6.6 pacing is 8-20s each, concurrency 1.\n", flush=True)

    result = run_social_enrichment(run_id, limit=args.limit)

    after = _counts(run_id)
    with session_scope() as session:
        run = session.get(Run, run_id)
        stats = (run.stats or {}).get("social_enrichment", {}) if run else {}
        status = run.status if run else "?"

    print("\n" + "=" * 66)
    print(f"Stage 3 - social enrichment (§6.4, §6.7)   status={status}")
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

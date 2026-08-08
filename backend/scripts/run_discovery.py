"""Run Stage 1 discovery from the command line.

The API and the §13 run form come in Phase 5; until then this is how you drive a
run. Also the fastest way to do the §16 validation pass, since it prints the
counts you need to sanity-check against §14.

    uv run python scripts/run_discovery.py --city Lahore --category salon \
        --synonyms 1 --tiles 3

Leave --synonyms/--tiles off for a full §5.1 fan-out (12 tiles × all synonyms),
which takes tens of minutes and makes hundreds of requests.
"""

from __future__ import annotations

import argparse

from sqlalchemy import func, select

from leadscraper.db.models import Business, Contact, Run
from leadscraper.db.session import session_scope
from leadscraper.enums import LineType, NumberPreference, RunStatus, WhatsAppLabel
from leadscraper.logging import configure_logging
from leadscraper.services.discovery import run_maps_discovery
from leadscraper.taxonomy import build_query_plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="Lahore")
    parser.add_argument("--category", default="salon")
    parser.add_argument("--synonyms", type=int, default=None, help="limit synonyms")
    parser.add_argument("--tiles", type=int, default=None, help="limit commercial tiles")
    parser.add_argument(
        "--number-preference", default=NumberPreference.OWNER_FIRST, choices=list(NumberPreference)
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    args = parser.parse_args()

    configure_logging()

    plan = build_query_plan(args.city, args.category, args.synonyms, args.tiles)
    print(f"Plan: {len(plan)} queries ({args.city} × {args.category})")
    for spec in plan[:5]:
        print(f"   {spec.query}")
    if len(plan) > 5:
        print(f"   ... and {len(plan) - 5} more")

    if args.dry_run:
        return 0

    with session_scope() as session:
        run = Run(
            city=args.city,
            category=args.category,
            number_pref=args.number_preference,
            sources_enabled={"google_maps": True},
            status=RunStatus.QUEUED,
        )
        session.add(run)
        session.flush()
        run_id = run.id
    print(f"\nRun {run_id} starting...\n")

    result = run_maps_discovery(run_id, synonym_limit=args.synonyms, tile_limit=args.tiles)

    with session_scope() as session:
        run = session.get(Run, run_id)
        businesses = session.scalar(
            select(func.count(Business.id)).where(Business.run_id == run_id)
        )
        contacts = session.scalar(
            select(func.count(Contact.id))
            .select_from(Contact)
            .join(Business)
            .where(Business.run_id == run_id)
        )
        mobiles = session.scalar(
            select(func.count(Contact.id))
            .select_from(Contact)
            .join(Business)
            .where(Business.run_id == run_id, Contact.line_type == LineType.MOBILE)
        )
        wa_likely = session.scalar(
            select(func.count(Contact.id))
            .select_from(Contact)
            .join(Business)
            .where(Business.run_id == run_id, Contact.wa_label != WhatsAppLabel.NO)
        )
        with_phone = session.scalar(
            select(func.count(func.distinct(Contact.business_id)))
            .select_from(Contact)
            .join(Business)
            .where(Business.run_id == run_id)
        )
        stats = (run.stats or {}).get("discovery", {}) if run else {}
        status = run.status if run else "?"

    print("\n" + "=" * 60)
    print(f"Run {run_id}  status={status}")
    print("=" * 60)
    for key, value in stats.items():
        print(f"  {key:24} {value}")
    print("-" * 60)
    print(f"  {'unique businesses':24} {businesses}")
    print(f"  {'with a phone':24} {with_phone}")
    print(f"  {'contacts':24} {contacts}")
    print(f"  {'mobiles':24} {mobiles}")
    print(f"  {'whatsapp likely/confirmed':24} {wa_likely}")
    print("=" * 60)
    print(f"\nStageResult: {result.as_dict()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

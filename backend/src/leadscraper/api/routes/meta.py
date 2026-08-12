"""Everything §13 Screen 1 needs to draw itself, plus the Settings screen.

The pickers read from ``taxonomy`` rather than from a list typed into the
frontend, so §3.1's city list and §4.2's synonym dictionary have exactly one
definition. §4.2 is called out in the doc as "the highest-leverage config" —
a second copy of it in TypeScript would be the fastest way to lose that.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter
from sqlalchemy import func, select

from leadscraper.api.deps import SessionDep
from leadscraper.api.routes.suppression import suppressed_contact_count
from leadscraper.api.schemas import BatchCatalogue, BatchInfo, EstimateRequest
from leadscraper.config import get_settings
from leadscraper.core import batches
from leadscraper.core.proxy import proxy_available
from leadscraper.db.models import DoNotContact
from leadscraper.enums import Category, NumberPreference, Stage
from leadscraper.pipeline.queues import queue_health
from leadscraper.pipeline.stages import implemented_stages, missing_stages
from leadscraper.services.estimates import estimate_run, slice_confirmation_rates
from leadscraper.taxonomy import (
    EXCLUDED_SOURCES,
    get_synonyms,
    load_cities,
    route_for,
)

router = APIRouter(prefix="/api/meta", tags=["meta"])


@router.get("/cities")
def cities() -> list[dict]:
    """§3.1 cities with their §5.1 commercial tiles — the tiles are what make
    volume achievable, and the count is what drives the query plan."""
    return [
        {
            "name": city.name,
            "tier": city.tier,
            "area_code": city.area_code,
            "tiles": list(city.tiles),
            "tile_count": len(city.tiles),
        }
        for city in sorted(load_cities().values(), key=lambda c: (c.tier, c.name))
    ]


@router.get("/categories")
def categories() -> list[dict]:
    """§4's seven verticals, each carrying its source routing.

    ``vertical_strength`` is the §4 finding that only three of the seven have a
    real vertical directory, surfaced rather than hidden: for ``none``, Maps
    carries the entire run and the operator should expect that.
    """
    out = []
    for category in Category:
        route = route_for(category)
        out.append(
            {
                "name": category.value,
                "synonyms": list(get_synonyms(category)),
                "synonym_count": len(get_synonyms(category)),
                "vertical_sources": [s.value for s in route.vertical],
                "vertical_strength": route.strength,
                "volume_drivers": [s.value for s in route.volume_driver],
            }
        )
    return out


@router.get("/batches", response_model=BatchCatalogue)
def batch_catalogue() -> BatchCatalogue:
    """_BATCH_SPEC.md §4's batches, in send-priority order.

    Served rather than typed into the frontend for §4.2's reason: a second copy
    of a controlled vocabulary is the fastest way to have the UI offer a filter
    the backend rejects. The response also carries what the cascade *does not*
    cover — ``categories`` is ``["food"]`` — because a picker showing seven
    batches with nothing in them looks broken, and the reason it is empty on a
    salon run is a fact about the spec rather than about the data.
    """
    return BatchCatalogue(
        batches=[BatchInfo(**asdict(batch)) for batch in batches.BATCHES],
        unbatched_slug=batches.UNBATCHED,
        categories=sorted(batches.BATCHED_CATEGORIES),
        note=(
            "Thresholds (200 reviews, rating 4.0) and the dine-in list are "
            "calibrated on one Lahore × food scrape. Businesses in every other "
            "category resolve to `unbatched`: the cascade has no definitions for "
            "them yet, and routing them by food's rules would pitch delivery "
            "commission at a salon. Defining another vertical means measuring "
            "its review-count percentiles, per §8 of the spec."
        ),
    )


@router.get("/number-preferences")
def number_preferences() -> list[dict]:
    """§3.3 — and the note that only one of the three filters."""
    return [
        {
            "value": NumberPreference.OWNER_FIRST,
            "label": "Owner / CEO first",
            "filters": False,
            "note": "named-person number → mobile w/ WA confirmed → mobile → landline",
        },
        {
            "value": NumberPreference.BUSINESS_FIRST,
            "label": "Business number first",
            "filters": False,
            "note": "main business line → mobile w/ WA confirmed → named-person → landline",
        },
        {
            "value": NumberPreference.WHATSAPP_ONLY,
            "label": "WhatsApp-verified only",
            "filters": True,
            # §3.3's note: the filter keeps the `likely` band deliberately.
            "note": (
                "The only preference that excludes numbers. Keeps the `likely` "
                "band — restricting to `confirmed` would cut the Islamabad run "
                "from 256 numbers to 53 and read as a broken run."
            ),
        },
    ]


@router.get("/stages")
def stages() -> dict:
    """Which of §2's six stages have a body. §16 phases named for the rest."""
    phases = {
        Stage.SOCIAL_ENRICHMENT.value: "Phase 8",
        Stage.PERSON_ATTRIBUTION.value: "Phase 9",
    }
    return {
        "implemented": [s.value for s in implemented_stages()],
        "missing": [
            {"stage": s.value, "phase": phases.get(s.value, "unscheduled")}
            for s in missing_stages()
        ],
    }


@router.post("/estimate")
def estimate(payload: EstimateRequest, session: SessionDep) -> dict:
    """§13 Screen 1's "Est. runtime · Est. available".

    Returns a *runtime* estimate always and an *availability* figure only where
    the exact slice has been run before. See ``services/estimates`` for why those
    are different kinds of question — §5.2 forbids extrapolating the second.
    """
    return estimate_run(
        session,
        payload.city,
        payload.category,
        synonym_limit=payload.synonym_limit,
        tile_limit=payload.tile_limit,
        enrich=payload.enrich,
        social=payload.social,
    ).as_dict()


@router.get("/settings")
def settings_view(session: SessionDep) -> dict:
    """§13's Settings screen — read-only.

    Secrets are reported as booleans, never echoed. Everything here resolves
    through ``config.Settings``, which §7 and §13 both point at as the single
    place these knobs live; editing them is still a ``.env`` change and a
    restart, which is honest for a single-operator tool and avoids a settings
    write path that could disagree with the file on disk.
    """
    config = get_settings()
    suppressions = session.scalar(select(func.count(DoNotContact.id))) or 0

    return {
        "pacing": {
            "concurrency": config.concurrency,
            "browser_workers": config.browser_workers,
            "delay_min_seconds": config.delay_min_seconds,
            "delay_max_seconds": config.delay_max_seconds,
            "circuit_break_failures": config.circuit_break_failures,
            "circuit_break_minutes": config.circuit_break_minutes,
        },
        "cache": {
            "listing_ttl_days": config.cache_ttl_listing_days,
            "detail_ttl_days": config.cache_ttl_detail_days,
            "archive_path": str(config.raw_archive_path),
        },
        "dedupe": {
            "fuzzy_threshold": config.dedupe_fuzzy_threshold,
            "corroborated_threshold": config.dedupe_corroborated_threshold,
        },
        "proxy": {
            "mode": config.proxy_mode,
            "configured": bool(config.proxy_url),
            "available": proxy_available(),
            "required_for": sorted(config.proxy_required_source_set),
            # §7.1 and §5.1: every measurement in this project was taken on a
            # direct connection, and Islamabad vs Lahore differed 45% vs 13% on
            # WhatsApp confirmation with identical code. Until a PK residential
            # proxy is in place, real market variation and Maps geo-ranking
            # against a non-PK IP cannot be separated.
            "caveat": (
                "No PK residential proxy configured. Maps geo-ranks results, so "
                "counts from a non-PK egress are indicative only (§5.1, §7.1)."
            )
            if not config.proxy_url
            else None,
        },
        "api_keys": {
            "serp": bool(config.serp_api_key),
            "meta": bool(config.meta_access_token),
        },
        "compliance": {
            "suppression_entries": suppressions,
            "contacts_currently_hidden": suppressed_contact_count(session),
        },
        "queue": queue_health(),
        "excluded_sources": EXCLUDED_SOURCES,
        "confirmation_rates_by_slice": slice_confirmation_rates(session),
    }

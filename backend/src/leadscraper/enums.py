"""Controlled vocabularies shared by the DB, the API and the scrapers.

These are the values that appear in ``implementation.md`` §3, §8, §9 and §11.
Keeping them in one place means a typo in a scraper is an ImportError, not a row
that quietly fails a filter three stages later.
"""

from __future__ import annotations

from enum import StrEnum


class Category(StrEnum):
    """§4 — the seven verticals."""

    FOOD = "food"
    REAL_ESTATE = "real_estate"
    ENTERTAINMENT = "entertainment"
    SALON = "salon"
    CAR_SERVICES = "car_services"
    FASHION = "fashion"
    ECOMMERCE = "ecommerce"


class NumberPreference(StrEnum):
    """§3.3 — controls ranking, not filtering, except ``WHATSAPP_ONLY``."""

    OWNER_FIRST = "owner_first"
    BUSINESS_FIRST = "business_first"
    WHATSAPP_ONLY = "whatsapp_only"


class Source(StrEnum):
    """Every module that can produce a contact. Written to ``contacts.source``."""

    GOOGLE_MAPS = "google_maps"
    BUSINESS_WEBSITE = "business_website"
    BUSINESSLIST_PK = "businesslist_pk"
    URDUPOINT = "urdupoint"
    BUSINESSBOOK_PK = "businessbook_pk"
    PAKPLAY = "pakplay"
    TURFY = "turfy"
    ZAMEEN = "zameen"
    FOODPANDA = "foodpanda"
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    SERP = "serp"
    BIO_LINK = "bio_link"
    OPERATOR = "operator"
    SEED = "seed"


class RunStatus(StrEnum):
    """§11 ``runs.status``."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


class RunMode(StrEnum):
    """§3.2 — discovery from city+category, or enrichment of a supplied seed list."""

    DISCOVERY = "discovery"
    SEED = "seed"


class SeedType(StrEnum):
    """§3.2 — determines which stage a seeded row enters at."""

    DOMAINS = "domains"
    NAMES_AND_CITY = "names_and_city"
    MAPS_URLS = "maps_urls"
    PLACE_IDS = "place_ids"


class ContactKind(StrEnum):
    PHONE = "phone"
    EMAIL = "email"


class LineType(StrEnum):
    """§9.2. ``UNKNOWN`` is honest — better than guessing mobile."""

    MOBILE = "mobile"
    LANDLINE = "landline"
    UAN = "uan"
    TOLL_FREE = "toll_free"
    UNKNOWN = "unknown"


class WhatsAppLabel(StrEnum):
    """§9.3 — the label is what the operator sees; the raw score stays internal."""

    CONFIRMED = "confirmed"
    LIKELY = "likely"
    NO = "no"


class PersonRole(StrEnum):
    """§8. ``LIKELY_OWNER`` is the Tier B inference and is never called confirmed."""

    OWNER = "owner"
    CEO = "ceo"
    DIRECTOR = "director"
    AGENT = "agent"
    MANAGER = "manager"
    LIKELY_OWNER = "likely_owner"


class Attribution(StrEnum):
    """§8 — how a name got joined to a number. Never fabricate the join."""

    CONFIRMED = "confirmed"
    LINKED = "linked"
    CO_OCCURRING = "co_occurring"
    INFERRED = "inferred"


class BelongsTo(StrEnum):
    """§12.1 column 14 — whose phone this is."""

    OWNER = "owner"
    BUSINESS = "business"
    AGENT = "agent"


class Stage(StrEnum):
    """§2 — the six pipeline stages, each an independent queue consumer."""

    DISCOVERY = "discovery"
    CONTACT_ENRICHMENT = "contact_enrichment"
    SOCIAL_ENRICHMENT = "social_enrichment"
    PERSON_ATTRIBUTION = "person_attribution"
    NORMALISE_SCORE = "normalise_score"
    DEDUPE_EXPORT = "dedupe_export"


class SourceStatus(StrEnum):
    """§7 circuit breaker states, surfaced as pills in the UI (§13 Screen 2)."""

    OK = "ok"
    THROTTLED = "throttled"
    BLOCKED = "blocked"
    CIRCUIT_OPEN = "circuit_open"
    DISABLED = "disabled"

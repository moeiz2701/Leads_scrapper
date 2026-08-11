"""Cities, commercial tiles, synonyms and source routing (§3.1, §4, §4.2, §5.1).

Loads the YAML config files and answers the two questions the discovery stage
asks: "which queries do I run?" and "which source modules apply?".
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

import yaml

from leadscraper.config import BACKEND_ROOT
from leadscraper.enums import Category, Source

CONFIG_DIR = BACKEND_ROOT / "config"


@dataclass(frozen=True, slots=True)
class City:
    name: str
    tier: int
    area_code: str
    tiles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceRoute:
    """§4 — which vertical sources exist for a category, and how good they are.

    The critical finding of §4 is that only three of the seven categories have a
    real vertical directory. ``strength`` keeps that honest instead of pretending
    every category has one: for ``none``, Maps carries the entire run.
    """

    vertical: tuple[Source, ...]
    strength: str  # strong | weak | none
    volume_driver: tuple[Source, ...]


# §4 category → source routing table, verbatim from the doc.
SOURCE_ROUTES: dict[Category, SourceRoute] = {
    Category.FOOD: SourceRoute(
        vertical=(Source.FOODPANDA,),
        strength="strong",
        volume_driver=(Source.GOOGLE_MAPS, Source.FOODPANDA),
    ),
    Category.REAL_ESTATE: SourceRoute(
        vertical=(Source.ZAMEEN,),
        strength="strong",
        volume_driver=(Source.ZAMEEN,),
    ),
    Category.ENTERTAINMENT: SourceRoute(
        vertical=(Source.PAKPLAY, Source.TURFY),
        strength="strong",
        volume_driver=(Source.PAKPLAY, Source.GOOGLE_MAPS),
    ),
    Category.SALON: SourceRoute(
        vertical=(),
        strength="none",
        volume_driver=(Source.GOOGLE_MAPS,),
    ),
    Category.CAR_SERVICES: SourceRoute(
        vertical=(),
        strength="weak",
        volume_driver=(Source.GOOGLE_MAPS,),
    ),
    Category.FASHION: SourceRoute(
        vertical=(),
        strength="none",
        volume_driver=(Source.GOOGLE_MAPS, Source.FACEBOOK, Source.INSTAGRAM),
    ),
    Category.ECOMMERCE: SourceRoute(
        vertical=(),
        strength="none",
        volume_driver=(Source.GOOGLE_MAPS, Source.FACEBOOK, Source.INSTAGRAM),
    ),
}

# §4.1 — recorded in code so a future contributor cannot quietly add a module for
# one of these without first reading why it was rejected.
EXCLUDED_SOURCES: dict[str, str] = {
    "linkedin": (
        "Near-zero phone data, aggressively anti-bot, actively litigates. Its only "
        "value is owner-name enrichment. Use the SECP register or the business's "
        "own About page instead."
    ),
    "tiktok": "Heavy JS, aggressive fingerprinting, negligible phone yield.",
    "khelpoint": "/venues serves 8 placeholder listings on stock imagery. Marketing site.",
    "cheetay": "Shut down completely, Jan 2024.",
    "apollo": "US/EU corporate data. Effectively no coverage of Pakistani SMBs.",
    "zoominfo": "US/EU corporate data. Effectively no coverage of Pakistani SMBs.",
    "hunter_io": "US/EU corporate data. Effectively no coverage of Pakistani SMBs.",
    "lusha": "US/EU corporate data. Effectively no coverage of Pakistani SMBs.",
    "daraz": "Marketplaces do not expose seller phone numbers.",
    "priceoye": "Marketplaces do not expose seller phone numbers.",
    "google_places_api": (
        "Deliberately unused: per-request pricing and quotas would cap the grid "
        "fan-out that makes the volume target reachable (§7.1)."
    ),
    # §5.3's other three directories, refused on measurement during Phase 6.
    # Recorded here for the same reason as §4.1's list: so nobody re-adds one
    # without first reading what the recon found. Reproduce with
    # `scripts/spike_directories.py`.
    "urdupoint": (
        "Named by §5.3 and §16's Phase 6 row, refused on measurement. §5.3 "
        "describes a 'clean field table (Mob #), mostly 03xx' with an "
        "owner-name field. Sampled over the 21 Lahore restaurant records its "
        "directory actually holds: 4% carry a mobile, 85% a landline, and 0% an "
        "owner name. §9.3 scores a landline 0.00 and §10.2 requires a mobile to "
        "qualify, so it cannot produce a qualified lead. It also costs one "
        "request and 171 KB per business against BusinessList's ~2.5 KB, "
        "because its listing pages carry no phone at all — and its taxonomy is "
        "industrial ('wool', 'neon sign mfrs'), with no salon or beauty "
        "category in it at any city."
    ),
    "businessbook_pk": (
        "§5.3 lists it as JS-rendered and warns on its coverage. It does not "
        "reach the point of needing a browser: /category/{slug}-{id} answers 500, "
        "and the root alternates between a 73 KB body and an empty one across "
        "consecutive requests. An unstable source does not earn the Playwright "
        "dependency §5.3 asks us to weigh."
    ),
    "yellowpage_pk": (
        "§5.3's own verdict is 'low quality, high duplication — use as "
        "corroboration only', and its category index is a 670 KB page whose "
        "listings are not in the served HTML. Refused rather than built as a "
        "second corroboration layer under a corroboration layer."
    ),
    "listing_com_pk": (
        "Serves a Cloudflare interstitial ('Checking your browser before "
        "accessing') on every path, including the root. That is an access "
        "control, and §5.5 draws the line there: automate interactions with "
        "public data, never automate past an access control."
    ),
}


def _load_yaml(name: str) -> dict:
    path = Path(CONFIG_DIR) / name
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@functools.lru_cache(maxsize=1)
def load_cities() -> dict[str, City]:
    return {
        name: City(
            name=name,
            tier=int(data.get("tier", 3)),
            area_code=str(data.get("area_code", "")),
            tiles=tuple(data.get("tiles", [])),
        )
        for name, data in _load_yaml("cities.yaml").items()
    }


@functools.lru_cache(maxsize=1)
def load_synonyms() -> dict[str, tuple[str, ...]]:
    return {k: tuple(v) for k, v in _load_yaml("synonyms.yaml").items()}


def get_city(name: str) -> City:
    cities = load_cities()
    if name not in cities:
        raise KeyError(f"Unknown city {name!r}. Known: {', '.join(sorted(cities))}")
    return cities[name]


def get_synonyms(category: Category | str, limit: int | None = None) -> tuple[str, ...]:
    key = category.value if isinstance(category, Category) else category
    synonyms = load_synonyms().get(key, ())
    return synonyms[:limit] if limit else synonyms


@dataclass(frozen=True, slots=True)
class QuerySpec:
    """One Maps query plus the provenance needed to attribute its results.

    The tile is not decoration: it becomes ``businesses.area`` (§12.1 column 5),
    and it is the only place that value can come from — a Maps result's address
    does not reliably name the commercial district.
    """

    query: str
    tile: str
    synonym: str
    city: str


def build_query_plan(
    city: str,
    category: Category | str,
    synonym_limit: int | None = None,
    tile_limit: int | None = None,
) -> list[QuerySpec]:
    """The §5.1 fan-out with tile/synonym provenance attached.

    The limits exist for smoke runs and for the §13 "estimated available"
    preview — a full plan is 12 tiles × N synonyms and takes tens of minutes.
    """
    city_cfg = get_city(city)
    synonyms = get_synonyms(category, synonym_limit)
    tiles = city_cfg.tiles[:tile_limit] if tile_limit else city_cfg.tiles
    return [
        QuerySpec(
            query=f"{synonym} in {tile}, {city_cfg.name}",
            tile=tile,
            synonym=synonym,
            city=city_cfg.name,
        )
        for tile in tiles
        for synonym in synonyms
    ]


def build_queries(
    city: str, category: Category | str, synonym_limit: int | None = None
) -> list[str]:
    """The §5.1 grid × synonym fan-out, as literal Maps search strings.

    ``12 tiles × 5 synonyms = 60 queries`` — this function is where that number
    comes from, so the run estimator and the discovery stage cannot disagree.
    """
    city_cfg = get_city(city)
    synonyms = get_synonyms(category, synonym_limit)
    return [
        f"{synonym} in {tile}, {city_cfg.name}"
        for tile in city_cfg.tiles
        for synonym in synonyms
    ]


def estimate_query_count(
    city: str, category: Category | str, synonym_limit: int | None = None
) -> int:
    return len(get_city(city).tiles) * len(get_synonyms(category, synonym_limit))


def route_for(category: Category | str) -> SourceRoute:
    key = Category(category) if isinstance(category, str) else category
    return SOURCE_ROUTES[key]

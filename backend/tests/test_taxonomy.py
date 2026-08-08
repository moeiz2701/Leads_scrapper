"""§3.1, §4, §4.2, §5.1 — config-driven taxonomy and query fan-out."""

from __future__ import annotations

import pytest

from leadscraper.enums import Category, Source
from leadscraper.taxonomy import (
    EXCLUDED_SOURCES,
    build_queries,
    estimate_query_count,
    get_city,
    get_synonyms,
    load_cities,
    load_synonyms,
    route_for,
)

# §3.1 lists these fifteen, tier 1 first.
DOC_CITIES = [
    "Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad", "Multan",
    "Peshawar", "Gujranwala", "Sialkot", "Hyderabad", "Quetta", "Bahawalpur",
    "Sargodha", "Gujrat", "Abbottabad",
]


def test_every_documented_city_is_configured() -> None:
    assert set(load_cities()) == set(DOC_CITIES)


def test_every_city_has_tiles() -> None:
    """§5.1: tiles are what make volume achievable. A city without them cannot
    reach the target and would fail silently at a low result count."""
    for name, city in load_cities().items():
        assert city.tiles, name
        assert city.area_code, name


def test_lahore_tiles_match_the_doc() -> None:
    """The only tile list implementation.md gives explicitly (§5.1)."""
    assert get_city("Lahore").tiles == (
        "DHA", "Gulberg", "Johar Town", "Model Town", "Bahria Town", "Faisal Town",
        "Garden Town", "Iqbal Town", "Township", "Cantt", "Wapda Town", "Valencia",
    )


def test_every_category_has_synonyms() -> None:
    synonyms = load_synonyms()
    for category in Category:
        assert synonyms.get(category.value), category


def test_salon_synonyms_include_local_transliterations() -> None:
    """§4.2's whole point: 'hajaam' and 'beauty parlour' reach listings that
    'barber' and 'salon' never do."""
    salon = get_synonyms(Category.SALON)
    assert "hajaam" in salon
    assert "beauty parlour" in salon
    assert "beauty parlor" in salon  # both spellings appear in real listings


def test_fashion_synonyms_include_urdu_terms() -> None:
    fashion = get_synonyms(Category.FASHION)
    assert "darzi" in fashion
    assert "ladies tailor" in fashion


# --------------------------------------------------------------------------- #
# §5.1 grid × synonym fan-out
# --------------------------------------------------------------------------- #


def test_query_count_is_tiles_times_synonyms() -> None:
    city = get_city("Lahore")
    assert estimate_query_count("Lahore", Category.SALON, synonym_limit=5) == len(city.tiles) * 5


def test_doc_worked_example_produces_60_queries() -> None:
    """§14: 'Maps fan-out (12 tiles × 5 synonyms) = 60 queries'."""
    assert estimate_query_count("Lahore", Category.SALON, synonym_limit=5) == 60


def test_build_queries_shape() -> None:
    queries = build_queries("Lahore", Category.SALON, synonym_limit=2)
    assert len(queries) == len(get_city("Lahore").tiles) * 2
    assert "salon in DHA, Lahore" in queries
    assert len(set(queries)) == len(queries)


def test_unknown_city_fails_loudly() -> None:
    with pytest.raises(KeyError, match="Unknown city"):
        get_city("Atlantis")


# --------------------------------------------------------------------------- #
# §4 routing and §4.1 exclusions
# --------------------------------------------------------------------------- #


def test_only_three_categories_have_a_strong_vertical() -> None:
    """§4's critical finding — build the router to reflect it rather than
    pretending every category has a vertical directory."""
    strong = {c for c in Category if route_for(c).strength == "strong"}
    assert strong == {Category.FOOD, Category.REAL_ESTATE, Category.ENTERTAINMENT}


def test_maps_is_the_volume_driver_where_no_vertical_exists() -> None:
    for category in (Category.SALON, Category.CAR_SERVICES, Category.FASHION, Category.ECOMMERCE):
        assert Source.GOOGLE_MAPS in route_for(category).volume_driver, category
        assert route_for(category).vertical == () or route_for(category).strength != "strong"


@pytest.mark.parametrize(
    "excluded",
    ["linkedin", "tiktok", "khelpoint", "cheetay", "apollo", "hunter_io",
     "daraz", "priceoye", "google_places_api"],
)
def test_exclusions_carry_a_reason(excluded: str) -> None:
    """§4.1 exists 'so nobody re-litigates these mid-build' — the reason has to
    travel with the exclusion, not just the fact of it."""
    assert excluded in EXCLUDED_SOURCES
    assert len(EXCLUDED_SOURCES[excluded]) > 20


def test_no_excluded_source_has_a_module_enum() -> None:
    """A Source enum member is a promise that a module exists. LinkedIn and
    TikTok must never acquire one."""
    source_values = {s.value for s in Source}
    for banned in ("linkedin", "tiktok", "khelpoint", "cheetay", "apollo", "hunter_io"):
        assert banned not in source_values

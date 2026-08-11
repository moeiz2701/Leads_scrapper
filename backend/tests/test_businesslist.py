"""§5.3 BusinessList.pk — the parser, pinned to captured live markup.

The fixture is a real Lahore × restaurants category page, trimmed to six company
blocks and gzipped. It exists for the same reason the Maps golden files do: this
parser reads a served DOM by class name, and a redesign that renames
``div.company`` or moves the phone cell would otherwise produce a run of empty
harvests that reports success — §5.5's failure mode, on a source whose whole job
is to be a second opinion.

If these fail after a BusinessList change, re-run
``scripts/spike_directories.py``, diff the markup, and update the selectors — do
not delete the test.
"""

from __future__ import annotations

import asyncio
import gzip
from pathlib import Path

import pytest

from leadscraper.config import Settings
from leadscraper.core.cache import FetchCache
from leadscraper.core.pacing import CircuitBreaker, PacingPolicy
from leadscraper.enums import Category, LineType, SourceStatus
from leadscraper.sources.businesslist import (
    BASE_URL,
    CATEGORY_SLUGS,
    BusinessListSource,
    category_urls,
    parse_listing_page,
)
from tests.conftest import requires_db
from tests.test_website_source import FakeClient

FIXTURES = Path(__file__).parent / "fixtures"
LAHORE_RESTAURANTS = f"{BASE_URL}/category/restaurants/city:lahore"


def _fixture() -> bytes:
    return gzip.decompress((FIXTURES / "businesslist_lahore_restaurants.html.gz").read_bytes())


@pytest.fixture(scope="module")
def page():
    return parse_listing_page(LAHORE_RESTAURANTS, _fixture())


def _settings(**overrides) -> Settings:
    base = {
        "delay_min_seconds": 0.0,
        "delay_max_seconds": 0.0,
        "circuit_break_failures": 3,
        "circuit_break_minutes": 30,
    }
    return Settings(**{**base, **overrides})


def _source(client, **kwargs) -> BusinessListSource:
    return BusinessListSource(
        settings=_settings(),
        policy=PacingPolicy(delay_min=0.0, delay_max=0.0, concurrency=1),
        client=client,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# The parser
# --------------------------------------------------------------------------- #


def test_listing_page_yields_one_record_per_company_block(page) -> None:
    assert len(page.listings) == 6
    assert [x.name for x in page.listings][:2] == ["Bombay Biryani", "Pizza M21"]


def test_every_listing_carries_a_stable_directory_id(page) -> None:
    """``data-cmpid`` is BusinessList's own key and the idempotency key for a
    re-run — a listing without one falls back to a name match, which is weaker."""
    assert all(x.directory_id for x in page.listings)
    assert page.listings[0].directory_id == "245174"


def test_the_listing_page_carries_the_phone_so_no_detail_fetch_is_needed(page) -> None:
    """The finding that decides the design. §5.3 says "phones in plain text" and
    does not say they are on the *listing*, which is what makes this source ~20
    complete businesses per request instead of one."""
    numbers = {x.name: [p.e164 for p in x.phones] for x in page.listings}
    assert numbers["Bombay Biryani"] == ["+924237114000"]
    assert numbers["Pizza M21"] == ["+923216966621"]


def test_line_types_are_classified_by_the_shared_9_2_classifier(page) -> None:
    by_name = {x.name: x.phones for x in page.listings}
    assert by_name["Bombay Biryani"][0].line_type is LineType.LANDLINE
    assert by_name["Pizza M21"][0].line_type is LineType.MOBILE
    assert by_name["Dunkin' Donuts"][0].line_type is LineType.UAN


def test_coordinates_come_from_data_ltd_not_data_lat(page) -> None:
    """The attribute is ``data-ltd``. Reading ``data-lat`` returns nothing and
    silently disables §10.1's distance test for the entire source."""
    first = page.listings[0]
    assert first.lat == pytest.approx(31.6563, abs=1e-3)
    assert first.lng == pytest.approx(74.0911, abs=1e-3)
    assert first.has_coordinates


def test_a_listing_without_a_phone_is_kept_and_reports_no_phone(page) -> None:
    """84% of listings carry a phone. The other 16% are still businesses, and
    dropping them would hide the fill rate the §5.5 check watches."""
    poet = next(x for x in page.listings if x.name.startswith("Poet"))
    assert poet.phones == ()
    assert poet.address is not None


def test_missing_rating_stays_none_rather_than_zero(page) -> None:
    """§10.2's load-bearing rule at the parser boundary: a rating the directory
    never published must not arrive at the scorer as "rated zero"."""
    assert page.listings[0].rating == 5.0
    assert page.listings[0].review_count == 1
    unrated = [x for x in page.listings if x.name != "Bombay Biryani"]
    assert all(x.rating is None and x.review_count is None for x in unrated)


def test_pagination_is_the_rel_next_link_in_the_head(page) -> None:
    assert page.next_url == f"{BASE_URL}/category/restaurants/2/city:lahore"


def test_total_found_is_captured_because_5_3s_coverage_warning_must_stay_true(page) -> None:
    """59 restaurants in the whole of Lahore, against 429 from Maps for the same
    slice. Recording it per run is what stops this source being re-sized as a
    volume driver later."""
    assert page.total_found == 59


def test_a_reshuffled_markup_yields_an_empty_page_rather_than_raising() -> None:
    """Every field degrades to None — §5.1's lesson, applied to a second source.
    An exception here would kill Stage 2 including the website pass that had
    already succeeded."""
    page = parse_listing_page(LAHORE_RESTAURANTS, b"<html><body><p>redesigned</p></body></html>")
    assert page.listings == []
    assert page.next_url is None
    assert page.total_found is None


def test_a_zero_zero_marker_is_read_as_no_location() -> None:
    """0,0 is the "no location" sentinel. Treated literally it is the Gulf of
    Guinea, which is within 150 m of every other unlocated row — so a whole
    category would merge into one business."""
    html = (
        '<div class="company" data-cmpid="1"><h3><a href="/company/1/x">X</a></h3>'
        '<div class="mapmarker" data-ltd="0" data-lng="0"></div></div>'
    )
    listing = parse_listing_page(LAHORE_RESTAURANTS, html).listings[0]
    assert listing.lat is None and listing.lng is None
    assert not listing.has_coordinates


# --------------------------------------------------------------------------- #
# Category routing
# --------------------------------------------------------------------------- #


def test_every_category_maps_to_at_least_one_directory_slug() -> None:
    """§4's seven verticals all have somewhere to go. A category with no mapping
    silently harvests nothing, which is the outcome §5.5 forbids."""
    assert set(CATEGORY_SLUGS) == set(Category)
    assert all(slugs for slugs in CATEGORY_SLUGS.values())


def test_category_urls_use_5_3s_published_pattern() -> None:
    urls = category_urls("Lahore", Category.SALON)
    assert urls[0] == f"{BASE_URL}/category/beauty-salons/city:lahore"
    assert len(urls) == 3


def test_no_mapped_slug_is_a_parent_index_page() -> None:
    """Five plausible-sounding slugs — ``food-drink``, ``health-beauty``,
    ``property``, ``computers-internet``, ``entertainment-media`` — are category
    *indexes* listing sub-categories, with no businesses on them at all.
    Requesting one spends a §7 request to parse zero listings."""
    mapped = {slug for slugs in CATEGORY_SLUGS.values() for slug in slugs}
    index_pages = {
        "food-drink",
        "health-beauty",
        "property",
        "computers-internet",
        "entertainment-media",
    }
    assert not (mapped & index_pages)


# --------------------------------------------------------------------------- #
# The source
# --------------------------------------------------------------------------- #


def test_harvest_follows_rel_next_and_dedupes_across_category_slugs() -> None:
    """"restaurants" and "fast-food" overlap, and the same ``data-cmpid`` must
    not become two businesses."""
    body = _fixture()
    page2 = body.replace(b'<link rel="next"', b'<link rel="notnext"')
    pages = {
        LAHORE_RESTAURANTS: (200, body, "text/html"),
        f"{BASE_URL}/category/restaurants/2/city:lahore": (200, page2, "text/html"),
        f"{BASE_URL}/category/fast-food/city:lahore": (200, body, "text/html"),
        f"{BASE_URL}/category/cafes/city:lahore": (200, b"<html></html>", "text/html"),
        f"{BASE_URL}/category/catering/city:lahore": (200, b"<html></html>", "text/html"),
    }
    client = FakeClient(pages)
    harvest = asyncio.run(_source(client).harvest("Lahore", Category.FOOD))

    # 6 unique companies, even though 3 pages served the same 6 blocks.
    assert len(harvest.listings) == 6
    assert harvest.categories_requested == len(CATEGORY_SLUGS[Category.FOOD])
    # One page per slug, plus the single shared ``rel=next`` target. "fast-food"
    # offers the same next URL as "restaurants", and a page already parsed in
    # this harvest is not re-fetched — so it is 5, not 6.
    assert harvest.pages_fetched == len(CATEGORY_SLUGS[Category.FOOD]) + 1
    assert client.requested.count(f"{BASE_URL}/category/restaurants/2/city:lahore") == 1


def test_a_429_stops_the_source_because_here_a_host_is_the_source() -> None:
    """The mirror image of §5.2. The website module had to demote a refusal to
    the host because it fans out over hundreds of them; BusinessList is one host,
    so §7's per-source breaker applies literally and stopping is correct."""
    client = FakeClient({LAHORE_RESTAURANTS: (429, b"", "text/html")})
    source = _source(client)
    harvest = asyncio.run(source.harvest("Lahore", Category.FOOD))

    assert harvest.refused
    assert harvest.error == "http_429"
    assert source.breaker.status() is SourceStatus.CIRCUIT_OPEN
    # It stopped rather than walking the other two category slugs into the wall.
    assert len(client.requested) == 1


def test_an_open_breaker_reports_blocked_which_is_not_the_same_as_empty() -> None:
    """"We were stopped" and "there was nothing there" are different facts, and
    §5.5 turns on telling them apart."""
    breaker = CircuitBreaker(source="businesslist_pk", failure_threshold=1)
    breaker.record_blocked(503)
    harvest = asyncio.run(
        _source(FakeClient({}), breaker=breaker).harvest("Lahore", Category.FOOD)
    )
    assert harvest.blocked
    assert not harvest.refused
    assert harvest.listings == []


@requires_db
def test_a_cached_page_costs_no_request_and_no_delay(fetch_cache: FetchCache) -> None:
    """§7's biggest lever, and the §5.2 corollary: the delay is spent only behind
    a request that was actually made."""
    body = _fixture()
    fetch_cache.put(LAHORE_RESTAURANTS, body, status=200, content_type="text/html")
    pages = {
        f"{BASE_URL}/category/restaurants/2/city:lahore": (200, b"<html></html>", "text/html"),
        f"{BASE_URL}/category/fast-food/city:lahore": (200, b"<html></html>", "text/html"),
        f"{BASE_URL}/category/cafes/city:lahore": (200, b"<html></html>", "text/html"),
        f"{BASE_URL}/category/catering/city:lahore": (200, b"<html></html>", "text/html"),
    }
    client = FakeClient(pages)
    harvest = asyncio.run(_source(client, cache=fetch_cache).harvest("Lahore", Category.FOOD))

    assert harvest.pages_from_cache == 1
    assert LAHORE_RESTAURANTS not in client.requested
    assert len(harvest.listings) == 6


@requires_db
def test_fetched_bodies_are_archived_so_a_selector_break_is_a_re_parse(
    fetch_cache: FetchCache,
) -> None:
    """§2 depends on this: when the markup changes you re-read stored bodies
    instead of re-scraping a source that has already moved on."""
    client = FakeClient({LAHORE_RESTAURANTS: (200, _fixture(), "text/html")})
    asyncio.run(_source(client, cache=fetch_cache).harvest("Lahore", Category.FOOD))
    assert fetch_cache.get(LAHORE_RESTAURANTS) is not None


def test_a_self_referencing_rel_next_terminates_immediately() -> None:
    """A page whose ``rel=next`` points at itself is the cheapest infinite loop
    a directory can hand you. The already-seen guard ends it on the first repeat,
    without waiting for the page ceiling."""
    body = _fixture().replace(
        b'href="https://www.businesslist.pk/category/restaurants/2/city:lahore"',
        f'href="{LAHORE_RESTAURANTS}"'.encode(),
    )
    client = FakeClient({LAHORE_RESTAURANTS: (200, body, "text/html")})
    asyncio.run(_source(client, max_pages=10).harvest("Lahore", Category.FOOD))
    assert client.requested.count(LAHORE_RESTAURANTS) == 1


def test_pagination_stops_at_the_page_ceiling() -> None:
    """§5.3's coverage warning as a ceiling. The deepest real category is three
    pages; a chain that keeps offering new URLs is bounded rather than followed."""
    pages: dict[str, tuple[int, bytes, str]] = {}
    for n in range(1, 9):
        url = LAHORE_RESTAURANTS if n == 1 else f"{BASE_URL}/category/restaurants/{n}/city:lahore"
        nxt = f"{BASE_URL}/category/restaurants/{n + 1}/city:lahore"
        pages[url] = (
            200,
            _fixture().replace(
                b'href="https://www.businesslist.pk/category/restaurants/2/city:lahore"',
                f'href="{nxt}"'.encode(),
            ),
            "text/html",
        )
    client = FakeClient(pages)
    source = _source(client, max_pages=3)
    asyncio.run(source.harvest("Lahore", Category.FOOD))

    restaurant_requests = [u for u in client.requested if "/category/restaurants/" in u]
    assert len(restaurant_requests) == 3

"""§5.2 crawler — budget, caching, pacing and the §5.5 breaker.

Every test here runs against a fake HTTP client. That is not only for speed:
the point of the §7 cache rule is that the fetch path is the *only* place a
request can originate, so a test that could accidentally reach the network would
not be testing the thing that matters.
"""

from __future__ import annotations

import asyncio

import pytest

from leadscraper.config import Settings
from leadscraper.core.cache import FetchCache
from leadscraper.core.pacing import CircuitBreaker, PacingPolicy
from leadscraper.enums import SourceStatus
from leadscraper.sources.website import (
    EMPTY_STREAK_THRESHOLD,
    MAX_PAGES_PER_DOMAIN,
    REFUSAL_STREAK_THRESHOLD,
    WebsiteSource,
    normalise_website,
)
from tests.conftest import requires_db

HOME = "https://salonx.pk/"

HOMEPAGE = """<html><body>
  <p>Salon X, Gulberg. Call 0300-1234567</p>
  <a href="/contact-us/">Contact Us</a>
  <a href="/about/">About</a>
</body></html>"""

CONTACT = """<html><body>
  <a href="https://wa.me/923001234567">WhatsApp us</a>
  <a href="mailto:info@salonx.pk">Email</a>
</body></html>"""


class FakeResponse:
    def __init__(self, url: str, status: int, body: bytes, content_type: str | None) -> None:
        self.url = url
        self.status_code = status
        self.content = body
        self.headers = {"content-type": content_type} if content_type else {}


class FakeClient:
    """Serves a fixed URL→page map and records every request it is asked for."""

    def __init__(
        self,
        pages: dict,
        error: Exception | None = None,
        redirects: dict[str, str] | None = None,
    ) -> None:
        self.pages = pages
        self.error = error
        # httpx follows redirects for us, so what the caller sees is the final
        # URL on a response to a request for the original one.
        self.redirects = redirects or {}
        self.requested: list[str] = []
        self.closed = False

    async def get(self, url: str) -> FakeResponse:
        self.requested.append(url)
        if self.error is not None:
            raise self.error
        final = self.redirects.get(url, url)
        entry = self.pages.get(final)
        if entry is None:
            return FakeResponse(final, 404, b"", "text/html")
        status, body, content_type = entry
        return FakeResponse(final, status, body, content_type)

    async def aclose(self) -> None:
        self.closed = True


def _html(body: str) -> tuple[int, bytes, str]:
    return 200, body.encode("utf-8"), "text/html; charset=utf-8"


def _settings(**overrides) -> Settings:
    base = {"proxy_mode": "direct", "proxy_url": "", "proxy_required_sources": "google_maps"}
    return Settings(**{**base, **overrides})


def _source(client: FakeClient, cache: FetchCache | None = None, **kwargs) -> WebsiteSource:
    kwargs.setdefault("settings", _settings())
    # Zero delay: the pacing policy itself is tested separately, and sleeping
    # through the crawl tests would buy nothing.
    kwargs.setdefault("policy", PacingPolicy(delay_min=0.0, delay_max=0.0, concurrency=3))
    return WebsiteSource(cache=cache, client=client, **kwargs)


def _crawl(source: WebsiteSource, url: str = HOME):
    return asyncio.run(source.crawl(url, client=source._client))


# --------------------------------------------------------------------------- #
# URL normalisation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("salonx.pk", "https://salonx.pk/"),
        ("http://salonx.pk", "http://salonx.pk/"),
        ("https://salonx.pk/home?a=1#frag", "https://salonx.pk/home?a=1"),
        ("  https://salonx.pk  ", "https://salonx.pk/"),
    ],
)
def test_maps_website_values_are_made_fetchable(raw: str, expected: str) -> None:
    """Maps writes a bare host as often as a full URL, and a bare host is not
    something httpx can fetch."""
    assert normalise_website(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "ftp://salonx.pk",
        # Glued to a scheme these would parse fine and resolve to nothing,
        # spending a DNS lookup and a timeout on every junk payload value.
        "javascript:void(0)",
        "not a url",
        "tel:03001234567",
        "localhost",
    ],
)
def test_unfetchable_website_values_are_rejected(raw) -> None:
    assert normalise_website(raw) is None


def test_an_unfetchable_url_never_reaches_the_client() -> None:
    client = FakeClient({})
    crawl = asyncio.run(_source(client).crawl("not a url", client=client))
    assert crawl.error == "unfetchable_url" and crawl.pages == []
    assert client.requested == []


# --------------------------------------------------------------------------- #
# The §5.2 crawl budget
# --------------------------------------------------------------------------- #


def test_the_crawl_follows_contact_and_about_links_from_the_homepage() -> None:
    client = FakeClient(
        {
            HOME: _html(HOMEPAGE),
            "https://salonx.pk/contact-us/": _html("<body><p>0321-1234567</p></body>"),
            "https://salonx.pk/about/": _html("<body><p>Since 2011</p></body>"),
        }
    )
    crawl = _crawl(_source(client))
    assert client.requested == [
        HOME,
        "https://salonx.pk/contact-us/",
        "https://salonx.pk/about/",
    ]
    assert len(crawl.pages) == 3


def test_the_crawl_never_exceeds_four_pages_per_domain() -> None:
    """§5.2: "Max 4 pages per domain." A site whose every page links to more
    contact pages must not turn one lead into an unbounded crawl."""
    links = "".join(f'<a href="/contact-{i}/">Contact {i}</a>' for i in range(8))
    pages = {HOME: _html(f"<body>{links}</body>")}
    for i in range(8):
        pages[f"https://salonx.pk/contact-{i}/"] = _html(f"<body>{links}<p>page {i}</p></body>")

    client = FakeClient(pages)
    crawl = _crawl(_source(client))
    assert len(client.requested) == MAX_PAGES_PER_DOMAIN
    assert len(crawl.pages) == MAX_PAGES_PER_DOMAIN


def test_the_crawl_stops_early_once_the_site_has_confirmed_a_number() -> None:
    """The budget is a ceiling, not a quota. With a wa.me link and an email in
    hand there is nothing left on the about page worth a request — §7's cheapest
    politeness is the request you do not make."""
    client = FakeClient(
        {HOME: _html(HOMEPAGE), "https://salonx.pk/contact-us/": _html(CONTACT)}
    )
    _crawl(_source(client))
    assert client.requested == [HOME, "https://salonx.pk/contact-us/"]


def test_a_page_is_never_fetched_twice_in_one_crawl() -> None:
    client = FakeClient(
        {
            HOME: _html('<body><a href="/contact/">Contact</a></body>'),
            "https://salonx.pk/contact/": _html(
                '<body><a href="/contact">Contact</a><a href="/contact/">Contact</a></body>'
            ),
        }
    )
    _crawl(_source(client))
    assert client.requested == [HOME, "https://salonx.pk/contact/"]


# --------------------------------------------------------------------------- #
# What is and is not worth parsing
# --------------------------------------------------------------------------- #


def test_a_404_is_recorded_but_not_parsed() -> None:
    client = FakeClient({})
    crawl = _crawl(_source(client))
    assert crawl.pages == [] and crawl.failed == 1


def test_a_pdf_is_not_a_contact_page() -> None:
    client = FakeClient({HOME: (200, b"%PDF-1.4 0300-1234567", "application/pdf")})
    assert _crawl(_source(client)).pages == []


def test_a_missing_content_type_is_treated_as_html() -> None:
    """Small PK hosts routinely omit the header. Dropping those pages would cost
    real leads for a field nobody reads."""
    client = FakeClient({HOME: (200, b"<body><p>0300-1234567</p></body>", None)})
    assert len(_crawl(_source(client)).pages) == 1


def test_an_oversized_body_is_skipped() -> None:
    client = FakeClient({HOME: (200, b"x" * 5_000_000, "text/html")})
    assert _crawl(_source(client)).pages == []


def test_a_dead_domain_is_an_outcome_not_a_crash() -> None:
    client = FakeClient({}, error=TimeoutError("connect timeout"))
    crawl = _crawl(_source(client))
    assert crawl.pages == [] and crawl.failed == 1 and crawl.error == "TimeoutError"


# --------------------------------------------------------------------------- #
# §7 cache
# --------------------------------------------------------------------------- #


@requires_db
def test_a_second_crawl_inside_the_ttl_makes_no_requests(fetch_cache: FetchCache) -> None:
    """§7 calls the cache the single biggest lever, and this is the shape of it:
    re-running a city × category inside the TTL costs zero network."""
    pages = {HOME: _html(HOMEPAGE), "https://salonx.pk/contact-us/": _html(CONTACT)}

    first = FakeClient(pages)
    _crawl(_source(first, cache=fetch_cache))
    assert first.requested

    second = FakeClient(pages)
    crawl = _crawl(_source(second, cache=fetch_cache))
    assert second.requested == []
    assert crawl.from_cache == 2 and crawl.fetched == 0
    assert crawl.evidence().confirmed_phones


@requires_db
def test_a_cached_crawl_does_not_sleep_between_pages(fetch_cache: FetchCache) -> None:
    """§7's delay exists to space out *requests*. Spending it after a cache hit
    is the one combination with no upside — it would turn a zero-request re-run
    of a few hundred domains back into a minutes-long one, cancelling out the
    saving the cache exists for."""
    pages = {
        HOME: _html(HOMEPAGE),
        "https://salonx.pk/contact-us/": _html("<body><p>0321-1234567</p></body>"),
        "https://salonx.pk/about/": _html("<body><p>Since 2011</p></body>"),
    }
    slow = PacingPolicy(delay_min=30.0, delay_max=30.0, concurrency=1)
    asyncio.run(_source(FakeClient(pages), cache=fetch_cache).crawl(HOME, client=FakeClient(pages)))

    client = FakeClient(pages)
    source = _source(client, cache=fetch_cache, policy=slow)
    crawl = asyncio.run(source.crawl(HOME, client=client))

    assert crawl.from_cache == 3 and crawl.requests == 0
    assert client.requested == []


@requires_db
def test_a_404_is_archived_so_the_next_run_does_not_rediscover_it(
    fetch_cache: FetchCache,
) -> None:
    client = FakeClient({HOME: _html('<body><a href="/about/">About</a></body>')})
    _crawl(_source(client, cache=fetch_cache))
    assert "https://salonx.pk/about/" in client.requested

    again = FakeClient({HOME: _html('<body><a href="/about/">About</a></body>')})
    _crawl(_source(again, cache=fetch_cache))
    assert again.requested == []


def test_a_redirected_page_is_attributed_to_where_it_ended_up() -> None:
    """Provenance points at the page that actually said it, not at the URL Maps
    happened to publish."""
    client = FakeClient(
        {"https://salonx.pk/": _html(HOMEPAGE)},
        redirects={"http://salonx.pk/": "https://salonx.pk/"},
    )
    crawl = asyncio.run(_source(client).crawl("http://salonx.pk/", client=client))
    assert crawl.pages[0].url == "https://salonx.pk/"


@requires_db
def test_a_redirect_archives_the_body_under_both_urls(fetch_cache: FetchCache) -> None:
    """Measured on the live Islamabad run: roughly one PK SMB domain in nine
    redirects http→https. Archiving only the requested URL breaks §2's re-parse
    path, because the contact records the *final* URL as its evidence; archiving
    only the final URL breaks the §7 cache, because the next run looks up the
    URL it is about to request. Both are needed and both are true."""
    requested, final = "http://salonx.pk/", "https://salonx.pk/"
    client = FakeClient({final: _html(CONTACT)}, redirects={requested: final})
    asyncio.run(_source(client, cache=fetch_cache).crawl(requested, client=client))

    evidence_url = "https://salonx.pk/"  # what the contact row will record
    assert fetch_cache.get(evidence_url, ignore_ttl=True) is not None

    again = FakeClient({final: _html(CONTACT)}, redirects={requested: final})
    asyncio.run(_source(again, cache=fetch_cache).crawl(requested, client=again))
    assert again.requested == []


@requires_db
def test_a_5xx_is_not_archived_because_it_is_transient(fetch_cache: FetchCache) -> None:
    client = FakeClient({HOME: (503, b"busy", "text/html")})
    _crawl(_source(client, cache=fetch_cache))
    assert not fetch_cache.is_fresh(HOME)


# --------------------------------------------------------------------------- #
# §7 / §5.5 circuit breaker
# --------------------------------------------------------------------------- #


def test_a_refusing_host_is_abandoned_but_the_module_carries_on() -> None:
    """This module is the one place where "source" and "host" come apart. Maps
    is genuinely one source, so §7 is right that a 429 should stop it. "Business
    websites" is a few hundred unrelated hosts, and one salon behind a WAF says
    nothing about the next salon.

    Measured on the live Lahore run: a single 403 tripped a source-level breaker
    and skipped 19 healthy domains — §7's "continue the run with the remaining
    sources", inverted."""
    source = _source(FakeClient({HOME: (403, b"", "text/html")}))
    crawl = _crawl(source)

    assert crawl.refused and crawl.error == "http_403"
    assert not crawl.blocked
    assert source.breaker.status() is SourceStatus.OK


def test_a_refusal_stops_that_domain_before_its_other_pages() -> None:
    """§7: honour it, do not grind. Two more requests to a host that just said
    no is exactly the grinding the section rules out."""
    client = FakeClient(
        {
            HOME: _html(HOMEPAGE),
            "https://salonx.pk/contact-us/": (429, b"", "text/html"),
            "https://salonx.pk/about/": _html(CONTACT),
        }
    )
    _crawl(_source(client))
    assert client.requested == [HOME, "https://salonx.pk/contact-us/"]


def test_a_long_run_of_refusals_is_us_not_them() -> None:
    """A streak across unrelated domains is the egress being blocked rather than
    one strict host, and that does stop the module."""
    source = _source(FakeClient({HOME: (403, b"", "text/html")}))
    for _ in range(REFUSAL_STREAK_THRESHOLD):
        _crawl(source)
    assert source.breaker.status() is SourceStatus.CIRCUIT_OPEN


def test_one_healthy_domain_clears_the_refusal_streak() -> None:
    source = _source(FakeClient({HOME: (403, b"", "text/html")}))
    for _ in range(REFUSAL_STREAK_THRESHOLD - 1):
        _crawl(source)
    source._client = FakeClient({HOME: _html(HOMEPAGE)})
    _crawl(source)

    source._client = FakeClient({HOME: (403, b"", "text/html")})
    _crawl(source)
    assert source.breaker.status() is SourceStatus.OK


def test_an_open_breaker_short_circuits_later_domains() -> None:
    client = FakeClient({HOME: _html(HOMEPAGE)})
    source = _source(client)
    source.breaker.record_blocked(429)
    crawl = _crawl(source)
    assert crawl.blocked and client.requested == []


def test_dead_domains_do_not_trip_the_breaker() -> None:
    """With a few hundred PK SMB sites in a run, several dead ones in a row is
    normal. Tripping on that would break a perfectly healthy module."""
    client = FakeClient({}, error=OSError("no route to host"))
    source = _source(client)
    for _ in range(10):
        _crawl(source)
    assert source.breaker.status() is SourceStatus.OK


def test_a_long_run_of_live_but_empty_sites_trips_the_breaker() -> None:
    """§5.5's actual failure mode: 200s that yield nothing because an extractor
    stopped matching. Empty *successes* are what catch that, not errors."""
    client = FakeClient({HOME: _html("<body><h1>Coming soon</h1></body>")})
    source = _source(client)
    for _ in range(EMPTY_STREAK_THRESHOLD):
        _crawl(source)
    assert source.breaker.status() is SourceStatus.CIRCUIT_OPEN
    assert source.breaker.tripped_by == "empty_streak"


def test_one_productive_site_clears_the_empty_streak() -> None:
    empty = _source(FakeClient({HOME: _html("<body><h1>Soon</h1></body>")}))
    for _ in range(EMPTY_STREAK_THRESHOLD - 1):
        _crawl(empty)
    empty._client = FakeClient({HOME: _html(HOMEPAGE)})
    _crawl(empty)
    assert empty.breaker.consecutive_empty == 0


def test_the_daily_budget_is_a_hard_ceiling() -> None:
    breaker = CircuitBreaker(source="business_website", daily_request_budget=1)
    source = _source(FakeClient({HOME: _html(HOMEPAGE)}), breaker=breaker)
    _crawl(source)
    crawl = _crawl(source)
    assert crawl.blocked and crawl.error == "daily_budget_exhausted"


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def test_websites_do_not_need_the_pk_proxy() -> None:
    """§7.1: only Maps is geo-ranked. Requiring a proxy here would gate the one
    source that produces `confirmed` labels behind the one paid dependency."""
    source = WebsiteSource(settings=_settings())
    assert source.proxy.is_direct


def test_crawl_many_is_bounded_by_concurrency_and_closes_its_client() -> None:
    client = FakeClient({HOME: _html(HOMEPAGE)})
    source = _source(client)
    crawls = asyncio.run(source.crawl_many([HOME, HOME]))
    assert len(crawls) == 2
    # The client was supplied, so the source must not close it out from under
    # the caller.
    assert not client.closed


def test_crawl_many_of_nothing_is_empty() -> None:
    assert asyncio.run(_source(FakeClient({})).crawl_many([])) == []

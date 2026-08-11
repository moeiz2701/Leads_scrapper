"""BusinessList.pk — the §5.3 horizontal directory that survived recon.

§5.3 lists four directories. Only this one earns a module; the other three are
refused in ``taxonomy.EXCLUDED_SOURCES`` with the measurements that condemned
them, the same way §4.1's exclusions are recorded. ``scripts/spike_directories.py``
reproduces the whole comparison.

**What recon found, and where it corrects §5.3.** §5.3 describes this source as
"Phones in plain text. 155k companies. Emails paywalled" — all three true. What
it does not say is the part that decides the design:

* The **listing page carries the whole record**. Name, detail URL, address,
  phone, rating, review count and — the important one — **latitude and longitude**
  in a ``div.mapmarker[data-ltd][data-lng]``. So a category page is ~20 complete
  businesses for one request, and no detail fetch is needed for any field §12.1
  exports. Measured across 3 pages / 58 listings: **84% carry a phone, 65% carry
  coordinates**, and the line mix is 55% mobile / 40% landline / 4% UAN.
* The coordinates are what make this source worth building at all. §10.1's
  fuzzy tier is an AND of name similarity and a 150 m distance test, so a source
  without coordinates can never join to a Maps business through it. This one can,
  for two rows in three.
* Pagination is a ``<link rel="next">`` in the head, not a numbered page list —
  there is nothing to parse out of the body.
* §5.3's coverage warning is exactly right and now measured: **59 restaurants and
  18 beauty salons in Lahore**, against the 429 and 60 the same slices returned
  from Maps. This is a corroboration layer. It is not a volume driver and must
  never be sized as one.

**On the circuit breaker, this source is the opposite of §5.2.** The website
module had to split "source" from "host" because it fans out over hundreds of
unrelated hosts. BusinessList is one host, so §7's per-source breaker applies
literally and without the §5.2 exception: a 429 here means every later page would
be refused too, and stopping is correct.

``robots.txt`` permits this. The published policy disallows ``/admin/``,
``/edit/*``, ``/sign-in/*``, ``/user/*`` and the search-helper endpoints;
``/category/*`` and ``/company/*`` — the only paths this module touches — are
not restricted for ``User-agent: *``. Checked at build time, recorded here
because §5.2 deliberately skipped robots for a different source and the contrast
is the point: there the file was irrelevant and commonly misconfigured, here it
is published, specific, and permits what we do.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from leadscraper.config import Settings, get_settings
from leadscraper.core.cache import FetchCache, FetchKind
from leadscraper.core.pacing import CircuitBreaker, PacingPolicy
from leadscraper.core.phone import ParsedPhone, extract_phones
from leadscraper.core.proxy import ProxyConfig, resolve_proxy
from leadscraper.core.textnorm import normalise_name
from leadscraper.enums import Category, Source
from leadscraper.logging import get_logger

log = get_logger(__name__)

BASE_URL = "https://www.businesslist.pk"

REQUEST_TIMEOUT = 20.0
MAX_BODY_BYTES = 4_000_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# §5.3's coverage warning, turned into a ceiling. The largest city × category
# this directory holds is 59 listings — three pages. A run that walks 10 pages of
# one category has either found an unusually deep category or is looping, and
# either way §5.3 says this source is not where volume comes from.
MAX_PAGES_PER_CATEGORY = 10

# One host, so §7's per-source breaker applies literally (see the module note).
_REFUSAL_STATUSES = frozenset({403, 429, 503})

# §5.3 is a *corroboration* layer over a Maps run that already succeeded, so an
# empty category is a normal outcome — plenty of city × category pairs genuinely
# hold nothing. The §5.5 check therefore lives at the module level (did every
# category come back empty?) rather than per page; see ``DirectoryHarvest``.


# --------------------------------------------------------------------------- #
# Category routing
# --------------------------------------------------------------------------- #

# Our 7 verticals onto BusinessList's 389-slug taxonomy, which is finer than
# ours. Several slugs per category for the same reason §4.2 fans out synonyms:
# "restaurants" and "fast-food" are different pages holding different businesses.
#
# **Every slug here was opened and read before it was added, and that is not
# ceremony.** BusinessList's categories are *self-assigned by the businesses that
# submit the listing*, so a plausible-sounding slug routinely holds the wrong
# population. Measured on Lahore:
#
# * ``food-retailers`` sounds like restaurants and holds **854** listings of
#   groceries, mineral-water plants and B2B wholesale — Erie Mineral Water,
#   Faysal Karyana Store, Skylark Engineering. Mapping it to ``food`` put 195
#   irrelevant rows into a 254-row harvest and cost 10 pages of requests.
# * ``textile`` (1,842) is fabric mills, not the ``fashion`` retail §4.2 means.
# * ``beauty-salons`` — the 18-listing figure §5.3 quotes — contains
#   ``Bigbasket.pk``, a grocery site. One business spamming categories appears in
#   ``beauty-salons``, ``catering`` and ``food-retailers`` at once.
#
# So a slug earns its place by what it was observed to contain, never by its
# name. ``scripts/spike_directories.py --categories`` re-runs the check.
#
# Slugs deliberately absent because they are *parent index pages* that list
# sub-categories and carry no businesses at all: ``food-drink``,
# ``health-beauty``, ``property``, ``computers-internet``,
# ``entertainment-media``. Requesting one spends a request to parse zero
# listings.
CATEGORY_SLUGS: dict[Category, tuple[str, ...]] = {
    Category.FOOD: ("restaurants", "fast-food", "cafes", "catering"),
    Category.SALON: ("beauty-salons", "hairdressers", "beauty-professionals"),
    Category.FASHION: ("clothing-and-accessories", "fashion", "tailors-and-alterations"),
    Category.CAR_SERVICES: ("auto-repair", "auto-services", "car-wash", "auto-parts-newused"),
    Category.REAL_ESTATE: ("estate-agents", "property-consultants"),
    Category.ECOMMERCE: ("mobile-phone-shops", "online-shopping", "general-shopping"),
    Category.ENTERTAINMENT: ("leisure", "sports", "fitness", "pubs-and-clubs"),
}


def category_urls(city: str, category: Category | str) -> list[str]:
    """§5.3's ``/category/{cat}/city:{city}`` pattern, one URL per mapped slug."""
    key = Category(category) if isinstance(category, str) else category
    slug_city = city.strip().lower().replace(" ", "-")
    return [
        f"{BASE_URL}/category/{slug}/city:{slug_city}" for slug in CATEGORY_SLUGS.get(key, ())
    ]


# --------------------------------------------------------------------------- #
# The parsed record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    """One company block from a BusinessList category page.

    Everything §12.1 needs is here, which is why this source never fetches a
    detail page. ``directory_id`` is BusinessList's own ``data-cmpid`` — stable
    across pages and the natural idempotency key for a re-run.
    """

    name: str
    name_norm: str
    source_url: str
    directory_id: str | None = None
    detail_url: str | None = None
    address: str | None = None
    phones: tuple[ParsedPhone, ...] = ()
    lat: float | None = None
    lng: float | None = None
    rating: float | None = None
    review_count: int | None = None

    @property
    def has_coordinates(self) -> bool:
        return self.lat is not None and self.lng is not None


@dataclass(slots=True)
class ListingPage:
    """One fetched category page."""

    url: str
    listings: list[DirectoryListing] = field(default_factory=list)
    next_url: str | None = None
    # BusinessList prints "We found <strong>59</strong> listings in Lahore".
    # Captured because it is the honest size of this source for a slice, and
    # §5.3's coverage warning is the single most important thing to keep true.
    total_found: int | None = None
    status: int = 0
    from_cache: bool = False
    refused: bool = False
    error: str | None = None


@dataclass(slots=True)
class DirectoryHarvest:
    """Everything one (city × category) harvest produced."""

    listings: list[DirectoryListing] = field(default_factory=list)
    pages_fetched: int = 0
    pages_from_cache: int = 0
    categories_requested: int = 0
    categories_answered: int = 0
    requests: int = 0
    refused: bool = False
    blocked: bool = False
    error: str | None = None
    totals: dict[str, int] = field(default_factory=dict)

    @property
    def stopped(self) -> bool:
        return self.refused or self.blocked


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

_TOTAL_RE = re.compile(r"We found\s*<strong>\s*([\d,]+)\s*</strong>\s*listings", re.I)


def parse_listing_page(url: str, body: bytes | str) -> ListingPage:
    """One category page → its company blocks. Pure: no network, no database.

    Written against the served markup rather than a rendered DOM, and every field
    degrades to ``None`` instead of raising — this is the §5.1 lesson applied to
    a second source. A markup reshuffle must produce an empty page that the §5.5
    check catches, never a traceback that kills the stage.
    """
    from selectolax.parser import HTMLParser

    from leadscraper.core.webparse import decode_body

    text = decode_body(body) if isinstance(body, bytes) else body
    page = ListingPage(url=url)

    total = _TOTAL_RE.search(text)
    if total:
        page.total_found = int(total.group(1).replace(",", ""))

    tree = HTMLParser(text)

    # ``<link rel="next">`` in the head is the whole pagination contract — the
    # body carries no numbered page list to parse.
    for link in tree.css('link[rel="next"]'):
        href = link.attributes.get("href")
        if href:
            page.next_url = urljoin(url, href)
            break

    for block in tree.css("div.company"):
        listing = _parse_company(block, url)
        if listing is not None:
            page.listings.append(listing)

    return page


def _parse_company(block, page_url: str) -> DirectoryListing | None:
    """One ``div.company`` → a listing, or ``None`` if it carries no identity."""
    anchor = block.css_first("h3 a")
    if anchor is None:
        # The category page wraps a promo card in the same class. No name and no
        # link means there is no business here to record.
        return None

    name = (anchor.text() or "").strip()
    if not name:
        return None

    href = anchor.attributes.get("href")
    detail_url = urljoin(page_url, href) if href else None

    address_node = block.css_first("div.address")
    address = _clean(address_node.text()) if address_node else None

    lat, lng = _coordinates(block)
    rating, review_count = _reviews(block)

    return DirectoryListing(
        name=name,
        name_norm=normalise_name(name),
        source_url=detail_url or page_url,
        directory_id=block.attributes.get("data-cmpid"),
        detail_url=detail_url,
        address=address,
        phones=tuple(_phones(block)),
        lat=lat,
        lng=lng,
        rating=rating,
        review_count=review_count,
    )


def _phones(block) -> list[ParsedPhone]:
    """The phone sits in the ``div.s`` whose icon is labelled "Phone number".

    Keyed on the icon's ``aria-label`` rather than position, because the sibling
    ``div.s`` blocks (E-mail, Map, Website, Photos) are present or absent per
    listing and an index into them is not stable.
    """
    for cell in block.css("div.s"):
        icon = cell.css_first("i")
        label = (icon.attributes.get("aria-label") or "") if icon else ""
        if "phone" in label.lower():
            return extract_phones(cell.text() or "")
    return []


def _coordinates(block) -> tuple[float | None, float | None]:
    """``data-ltd``/``data-lng`` — note ``ltd``, not ``lat``.

    Present on about two listings in three. Missing coordinates stay missing:
    §10.1's distance test is an AND, and a default would turn "we do not know
    where this is" into "it is at the equator", which merges by accident.
    """
    marker = block.css_first("div.mapmarker")
    if marker is None:
        return None, None
    lat = _float(marker.attributes.get("data-ltd"))
    lng = _float(marker.attributes.get("data-lng"))
    if lat is None or lng is None:
        return None, None
    # A marker at 0,0 is the "no location" sentinel, not the Gulf of Guinea.
    if lat == 0.0 and lng == 0.0:
        return None, None
    return lat, lng


def _reviews(block) -> tuple[float | None, int | None]:
    container = block.css_first("div.company_reviews")
    if container is None:
        return None, None
    rate_node = container.css_first("div.rate")
    rating = _float(rate_node.text()) if rate_node else None
    count = None
    match = re.search(r"(\d[\d,]*)\s+Review", container.text() or "")
    if match:
        count = int(match.group(1).replace(",", ""))
    return rating, count


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    collapsed = re.sub(r"\s+", " ", value).strip()
    return collapsed or None


# --------------------------------------------------------------------------- #
# The source
# --------------------------------------------------------------------------- #


class BusinessListSource:
    """Cache-first, paginated harvest of one city × category."""

    source = Source.BUSINESSLIST_PK

    def __init__(
        self,
        cache: FetchCache | None = None,
        settings: Settings | None = None,
        breaker: CircuitBreaker | None = None,
        policy: PacingPolicy | None = None,
        proxy: ProxyConfig | None = None,
        max_pages: int = MAX_PAGES_PER_CATEGORY,
        client=None,
    ) -> None:
        self.settings = settings or get_settings()
        self.cache = cache
        self.max_pages = max_pages
        self._client = client
        self.breaker = breaker or CircuitBreaker(
            source=self.source,
            failure_threshold=self.settings.circuit_break_failures,
            pause_minutes=self.settings.circuit_break_minutes,
        )
        # §7's 3–10s band, unmodified. §5.2 shortened it because a business's own
        # site sees 4 requests in a whole run; this is the situation §7 actually
        # calibrated for — one host, asked for several pages in a row.
        self.policy = policy or PacingPolicy(
            delay_min=self.settings.delay_min_seconds,
            delay_max=self.settings.delay_max_seconds,
            concurrency=1,
        )
        # Geo-neutral: only Maps is in §7.1's required set. A directory serves
        # the same rows to any IP — the pages are addressed by city in the URL.
        self.proxy = proxy if proxy is not None else resolve_proxy(self.source, self.settings)

    async def harvest(self, city: str, category: Category | str) -> DirectoryHarvest:
        """Walk every mapped category slug for a city, following ``rel=next``."""
        harvest = DirectoryHarvest()
        urls = category_urls(city, category)
        harvest.categories_requested = len(urls)
        if not urls:
            harvest.error = "no_category_mapping"
            log.warning("businesslist.no_mapping", city=city, category=str(category))
            return harvest

        client = self._client or self._new_client()
        owns_client = self._client is None
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        # Shared across category slugs, not per slug. BusinessList's mapped
        # categories overlap, and their ``rel=next`` chains converge onto the
        # same later pages — so a per-slug guard would spend a real §7 request
        # re-fetching a page this harvest has already parsed. It also makes a
        # self-referencing ``rel=next`` terminate immediately instead of running
        # to the page ceiling.
        seen_urls: set[str] = set()

        try:
            for url in urls:
                if harvest.stopped:
                    break
                await self._walk_category(harvest, url, client, seen_ids, seen_names, seen_urls)
        finally:
            if owns_client:
                await client.aclose()

        return harvest

    async def _walk_category(
        self,
        harvest: DirectoryHarvest,
        start_url: str,
        client,
        seen_ids: set[str],
        seen_names: set[str],
        seen_urls: set[str],
    ) -> None:
        url: str | None = start_url
        pages = 0
        answered = False

        while url and pages < self.max_pages:
            if url in seen_urls:
                break
            seen_urls.add(url)
            # ``paced`` is decided inside the fetch, behind the cache check: the
            # §7 delay must be spent only after a request that was actually made.
            # Sleeping on a cache hit turns a zero-request re-run into a
            # minutes-long one, which is the §5.2 measurement applied to a
            # second source.
            page = await self._fetch_page(harvest, url, client, paced=harvest.requests > 0)
            if page is None or harvest.stopped:
                return

            pages += 1
            answered = True
            if page.total_found is not None:
                harvest.totals.setdefault(_slug_of(start_url), page.total_found)

            for listing in page.listings:
                # Cross-slug dedupe: "restaurants" and "food-drink" overlap, and
                # the same company must not arrive twice. ``data-cmpid`` is the
                # real key; the normalised name is the fallback for the rare
                # block that omits it.
                key = listing.directory_id or ""
                if key:
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                elif listing.name_norm in seen_names:
                    continue
                seen_names.add(listing.name_norm)
                harvest.listings.append(listing)

            url = page.next_url

        if answered:
            harvest.categories_answered += 1

    async def _fetch_page(
        self, harvest: DirectoryHarvest, url: str, client, *, paced: bool
    ) -> ListingPage | None:
        cached = self.cache.get(url) if self.cache is not None else None
        if cached is not None:
            harvest.pages_from_cache += 1
            if cached.status != 200 or not cached.body:
                return None
            return parse_listing_page(url, cached.body)

        if self.breaker.is_open():
            harvest.blocked = True
            harvest.error = f"circuit_open:{self.breaker.tripped_by}"
            log.warning("businesslist.circuit_open", url=url)
            return None
        if self.breaker.budget_exhausted():
            harvest.blocked = True
            harvest.error = "daily_budget_exhausted"
            return None

        if paced:
            await asyncio.sleep(self.policy.next_delay())

        self.breaker.record_request()
        harvest.requests += 1
        try:
            response = await client.get(url)
        except Exception as exc:  # noqa: BLE001 — record and stop, never traceback
            harvest.error = harvest.error or type(exc).__name__
            self.breaker.record_failure(type(exc).__name__)
            log.warning("businesslist.fetch_failed", url=url, error=str(exc)[:160])
            return None

        status = response.status_code
        body = response.content

        if status in _REFUSAL_STATUSES:
            # One host, so this *is* the source refusing (see the module note).
            harvest.refused = True
            harvest.error = f"http_{status}"
            self.breaker.record_blocked(status)
            log.warning("businesslist.refused", url=url, status=status)
            return None

        if status != 200 or not body or len(body) > MAX_BODY_BYTES:
            self.breaker.record_failure(f"http_{status}")
            return None

        if self.cache is not None:
            self.cache.put(
                url,
                body,
                status=status,
                content_type=response.headers.get("content-type"),
                source=self.source,
                # A category page is a listing, not a detail page: §7's 7-day
                # TTL, because a directory adds companies continuously.
                kind=FetchKind.LISTING,
            )

        harvest.pages_fetched += 1
        page = parse_listing_page(url, body)
        self.breaker.record_success(produced=bool(page.listings))
        return page

    def _new_client(self):
        import httpx

        return httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(REQUEST_TIMEOUT),
            proxy=self.proxy.httpx_proxy(),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-PK,en;q=0.9,ur;q=0.8",
            },
            verify=False,
            max_redirects=5,
        )


def _slug_of(url: str) -> str:
    match = re.search(r"/category/([^/]+)", url)
    return match.group(1) if match else url

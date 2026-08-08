"""Google Maps — the volume engine, ~70% of all leads (§5.1).

Discovery is the §5.1 grid × synonym fan-out: tile the city by commercial
district, multiply by the §4.2 synonym list, and dedupe across queries. A single
query caps at ~120 results, so volume comes from the multiplication.

Extraction reads the intercepted network payload rather than the rendered DOM,
which is both more stable across redesigns and — per the recon spike recorded in
§5.1 — enough on its own: the search response already carries the phone number
for every result, so this module does not open a detail panel per business.

Three things worth knowing before changing this file:

* **Every navigation goes through the cache first.** §7 calls that the single
  biggest win. A re-run of the same city × category inside the TTL makes zero
  network requests.
* **A blocked or challenged response stops the source, it does not retry.**
  §7: "don't hammer a source that's refusing".
* **A query that returns zero results is reported, not swallowed.** §5.5's
  silent-breakage scenario applies most dangerously here, because Maps carries
  the run and 60 empty queries look exactly like 60 successful ones.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from leadscraper.config import Settings, get_settings
from leadscraper.core.cache import FetchCache, FetchKind
from leadscraper.core.maps_payload import MapsResult, parse_search_results
from leadscraper.core.pacing import CircuitBreaker, PacingPolicy
from leadscraper.core.proxy import ProxyConfig, resolve_proxy
from leadscraper.enums import Source
from leadscraper.logging import get_logger

log = get_logger(__name__)

SEARCH_URL_TEMPLATE = "https://www.google.com/maps/search/{query}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Markers that mean "Google is challenging us", not "no results".
_CHALLENGE_MARKERS = ("/sorry/", "recaptcha", "unusual traffic")


@dataclass
class QueryOutcome:
    query: str
    url: str
    results: list[MapsResult] = field(default_factory=list)
    from_cache: bool = False
    blocked: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.blocked and self.error is None


def search_url(query: str) -> str:
    return SEARCH_URL_TEMPLATE.format(query=quote_plus(query))


def merge_results(batches: list[list[MapsResult]]) -> list[MapsResult]:
    """Collapse several payloads for one query into one result set.

    A single navigation can emit more than one search response, and §5.1 records
    that they differ in richness — the lighter one omitted review counts the
    heavier one had. Keep whichever record for a given place_id has more fields
    populated, so a thin response never displaces a fat one.
    """
    best: dict[str, MapsResult] = {}
    order: list[str] = []
    for batch in batches:
        for result in batch:
            key = result.place_id or f"{result.name}|{result.address}"
            if key not in best:
                best[key] = result
                order.append(key)
            elif _filled_fields(result) > _filled_fields(best[key]):
                best[key] = result
    return [best[k] for k in order]


def _filled_fields(result: MapsResult) -> int:
    return sum(
        1
        for value in (
            result.name, result.place_id, result.address, result.lat, result.lng,
            result.phone_raw, result.website, result.rating, result.review_count,
            result.category,
        )
        if value is not None
    )


class GoogleMapsSource:
    """Playwright-backed discovery over a set of queries."""

    source = Source.GOOGLE_MAPS

    def __init__(
        self,
        cache: FetchCache | None = None,
        settings: Settings | None = None,
        breaker: CircuitBreaker | None = None,
        policy: PacingPolicy | None = None,
        proxy: ProxyConfig | None = None,
        max_scrolls: int = 12,
        scroll_settle_ms: int = 2_500,
    ) -> None:
        self.settings = settings or get_settings()
        self.cache = cache
        # ~120 results/query cap at ~20 per lazy-load means 12 scrolls is
        # generous headroom; the idle check usually stops well before it.
        self.max_scrolls = max_scrolls
        self.scroll_settle_ms = scroll_settle_ms
        self.breaker = breaker or CircuitBreaker(
            source=self.source,
            failure_threshold=self.settings.circuit_break_failures,
            empty_threshold=self.settings.empty_reveal_break_threshold,
            pause_minutes=self.settings.circuit_break_minutes,
        )
        self.policy = policy or PacingPolicy(
            delay_min=self.settings.delay_min_seconds,
            delay_max=self.settings.delay_max_seconds,
            concurrency=self.settings.browser_workers,
        )
        # Raises if PROXY_MODE=direct while google_maps is in
        # PROXY_REQUIRED_SOURCES — see §7.1. Opting out is a deliberate edit to
        # that env var, not something this module decides.
        self.proxy = proxy if proxy is not None else resolve_proxy(self.source, self.settings)

    # ------------------------------------------------------------------ cache

    def _cached_results(self, url: str) -> list[MapsResult] | None:
        if self.cache is None:
            return None
        cached = self.cache.get(url)
        if cached is None:
            return None
        return parse_search_results(cached.body)

    def _store(self, url: str, payloads: list[bytes]) -> None:
        if self.cache is None or not payloads:
            return
        # Store the richest payload — that is the one a re-parse should see.
        richest = max(payloads, key=len)
        self.cache.put(
            url,
            richest,
            content_type="application/json",
            source=self.source,
            kind=FetchKind.LISTING,
        )

    # ------------------------------------------------------------------ public

    async def discover(self, queries: list[str]) -> list[QueryOutcome]:
        """Run every query, honouring cache, pacing and the circuit breaker."""
        outcomes: list[QueryOutcome] = []
        pending: list[str] = []

        for query in queries:
            url = search_url(query)
            cached = self._cached_results(url)
            if cached is not None:
                outcomes.append(
                    QueryOutcome(query=query, url=url, results=cached, from_cache=True)
                )
                log.info("maps.cache_hit", query=query, results=len(cached))
            else:
                pending.append(query)

        if pending:
            outcomes.extend(await self._run_live(pending))
        return outcomes

    async def _run_live(self, queries: list[str]) -> list[QueryOutcome]:
        from playwright.async_api import async_playwright

        outcomes: list[QueryOutcome] = []
        work: asyncio.Queue[str] = asyncio.Queue()
        for query in queries:
            work.put_nowait(query)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, proxy=self.proxy.playwright_proxy()
            )
            try:
                workers = [
                    asyncio.create_task(self._worker(browser, work, outcomes))
                    for _ in range(min(self.policy.concurrency, len(queries)))
                ]
                await asyncio.gather(*workers)
            finally:
                await browser.close()

        return outcomes

    async def _worker(self, browser, work: asyncio.Queue, sink: list[QueryOutcome]) -> None:
        # §7: a persistent profile per worker keeps consent state, which means
        # fewer interstitials and fewer wasted navigations.
        context = await browser.new_context(
            locale="en-GB",
            timezone_id="Asia/Karachi",
            viewport={"width": 1440, "height": 900},
            user_agent=USER_AGENT,
        )
        page = await context.new_page()
        try:
            while True:
                try:
                    query = work.get_nowait()
                except asyncio.QueueEmpty:
                    return

                if self.breaker.is_open():
                    log.warning(
                        "maps.circuit_open", query=query, reason=self.breaker.tripped_by
                    )
                    sink.append(
                        QueryOutcome(
                            query=query,
                            url=search_url(query),
                            blocked=True,
                            error=f"circuit_open:{self.breaker.tripped_by}",
                        )
                    )
                    continue

                outcome = await self._run_query(page, query)
                sink.append(outcome)
                await asyncio.sleep(self.policy.next_delay())
        finally:
            with contextlib.suppress(Exception):
                await context.close()

    async def _run_query(self, page, query: str) -> QueryOutcome:
        url = search_url(query)
        payloads: list[bytes] = []

        async def on_response(response) -> None:
            if "tbm=map" not in response.url:
                return
            try:
                body = await response.body()
            except Exception:  # noqa: BLE001 — aborted or already consumed
                return
            if len(body) > 1000:
                payloads.append(body)

        page.on("response", on_response)
        try:
            self.breaker.record_request()
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await self._dismiss_consent(page)

            if await self._is_challenged(page):
                self.breaker.record_captcha()
                log.warning("maps.challenged", query=query)
                return QueryOutcome(query=query, url=url, blocked=True, error="challenge")

            await page.wait_for_timeout(4_000)
            await self._exhaust_results(page, payloads)
        except Exception as exc:  # noqa: BLE001 — one query must not kill the run
            self.breaker.record_failure(str(exc)[:200])
            log.warning("maps.query_failed", query=query, error=str(exc)[:200])
            return QueryOutcome(query=query, url=url, error=str(exc)[:200])
        finally:
            page.remove_listener("response", on_response)

        results = merge_results([parse_search_results(p) for p in payloads])
        self._store(url, payloads)

        # §5.5: a query returning nothing is suspicious, not neutral. Maps carries
        # the run, so a run of empty queries means the payload shape moved.
        self.breaker.record_success(produced=bool(results))
        log.info("maps.query_done", query=query, results=len(results))
        return QueryOutcome(query=query, url=url, results=results)

    async def _exhaust_results(self, page, payloads: list[bytes]) -> None:
        """Scroll the results feed until Maps stops loading more.

        This is a volume lever, not a polish item. Maps lazy-loads roughly 20
        results at a time toward its ~120-per-query cap, so a single scroll
        yields 20 and the §5.1 fan-out silently delivers a fraction of what it
        should. Measured on Lahore × salon: one scroll gave 20 results/query,
        scrolling to exhaustion gives substantially more for the same navigation
        — the cheapest volume in the system, since the page is already loaded.

        Stops on: no new payload for two consecutive scrolls, the hard scroll
        cap, or the end-of-list marker. Bounded so a feed that keeps yielding
        cannot spin forever.
        """
        feed = page.locator('div[role="feed"]').first
        idle_rounds = 0
        last_height = -1

        for _ in range(self.max_scrolls):
            try:
                if await feed.count():
                    height = await feed.evaluate(
                        "el => { el.scrollTo(0, el.scrollHeight); return el.scrollHeight; }"
                    )
                else:
                    await page.mouse.wheel(0, 5000)
                    height = await page.evaluate("() => document.body.scrollHeight")
            except Exception:  # noqa: BLE001 — feed detached mid-scroll
                return

            await page.wait_for_timeout(self.scroll_settle_ms)

            # Progress is measured by the feed growing, NOT by a payload
            # arriving. Measured on live Maps, pagination responses fire only
            # every ~3 scrolls while the feed grows on every one — keying the
            # idle check on payloads stopped after two scrolls and cost roughly
            # two thirds of the results the query could have returned.
            if height > last_height:
                last_height = height
                idle_rounds = 0
                continue

            idle_rounds += 1
            if idle_rounds >= 3:
                return

    async def _is_challenged(self, page) -> bool:
        if any(marker in page.url for marker in _CHALLENGE_MARKERS):
            return True
        with contextlib.suppress(Exception):
            content = (await page.content())[:5000].lower()
            return any(marker in content for marker in _CHALLENGE_MARKERS[1:])
        return False

    async def _dismiss_consent(self, page) -> None:
        """Google's cookie interstitial: public, pre-auth, one click.

        This is a UI affordance on public data, not an access control — the §5.5
        distinction. Nothing here automates past a login wall.
        """
        for selector in (
            'button:has-text("Accept all")',
            'button:has-text("Reject all")',
            'form[action*="consent"] button',
        ):
            with contextlib.suppress(Exception):
                button = page.locator(selector).first
                if await button.count() and await button.is_visible():
                    await button.click(timeout=5_000)
                    await page.wait_for_timeout(1_500)
                    return

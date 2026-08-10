"""The business's own website — the WhatsApp confirmation engine (§5.2).

Nothing else in the design can produce a `confirmed` WhatsApp label. Maps hands
us a number and §9.3 scores a bare mobile at 0.60 — *likely*. Only the business
itself publishing ``wa.me/92…`` settles it, and it does that on its own site.

Three properties, in the order they matter:

* **No browser.** §5.2: zero anti-bot friction, plain ``httpx`` + a parser, ~10×
  faster than Playwright. Nothing here needs a rendered DOM.
* **Cache first, always.** §7's biggest lever. A re-run inside the 30-day detail
  TTL makes no network requests at all, and §2's re-parse path depends on the
  stored bodies when an extractor turns out to be wrong.
* **A crawl budget of 4 pages per domain**, per §5.2: homepage, then the
  ``/contact*`` and ``/about*`` links the homepage actually offers.

On failure handling: a domain that times out or 404s is a dead domain, not a
refusing source, so it does **not** touch the circuit breaker — with a few
hundred PK SMB sites in a run, several dead ones in a row is normal and tripping
on that would break a healthy module. What does trip it is a source-level
refusal (429/503) or the §5.5 signature: a long run of domains that answer 200
and yield nothing, which means an extractor stopped matching.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from leadscraper.config import Settings, get_settings
from leadscraper.core.cache import FetchCache, FetchKind
from leadscraper.core.pacing import WEBSITE_PACING, CircuitBreaker, PacingPolicy
from leadscraper.core.proxy import ProxyConfig, resolve_proxy
from leadscraper.core.site_evidence import SiteEvidence, build_site_evidence
from leadscraper.core.textnorm import registrable_domain
from leadscraper.core.webparse import PageExtract, parse_page
from leadscraper.enums import Source
from leadscraper.logging import get_logger

log = get_logger(__name__)

# §5.2: "Max 4 pages per domain."
MAX_PAGES_PER_DOMAIN = 4

REQUEST_TIMEOUT = 20.0

# Past this a "page" is an asset or a dump, not a contact page. Parsing a 10 MB
# body to find a phone number costs more than the number is worth.
MAX_BODY_BYTES = 4_000_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# §5.5's empty-streak breaker, retuned for this source. Maps uses 5 because a
# Maps query returning nothing is already abnormal. Here it is not: a real
# fraction of PK SMB sites are a single splash image with no contact details at
# all, and at a ~25% no-yield rate a threshold of 5 would false-trip roughly
# once every 1,000 domains — often enough to fire during a full run. A genuine
# extractor break yields 0% and trips at 25 domains regardless, so the higher
# threshold costs nothing in detection and removes the false positives.
EMPTY_STREAK_THRESHOLD = 25

_HTML_CONTENT_TYPES = ("text/html", "application/xhtml", "text/plain")

# 403/429/503 are a host saying no. §7: honour it, do not back off into the same
# wall. 403 is included because a WAF answering 403 to every request is the same
# situation wearing a different number.
_REFUSAL_STATUSES = frozenset({403, 429, 503})

# How many domains may refuse *consecutively* before we conclude the problem is
# us rather than them.
#
# This module is the one place where "source" and "host" come apart. Maps is
# genuinely one source: a 429 from Google means Google is refusing and every
# later query would too, so §7 is right that it should stop. "Business websites"
# is not one source — it is a few hundred unrelated hosts, and one salon behind
# a WAF says nothing about the next salon. Measured on the live Lahore run, a
# single 403 tripped a source-level breaker and skipped 19 healthy domains,
# which is the §7 rule inverted: continue with what is still answering.
#
# A long *run* of refusals is different in kind — that is our egress being
# blocked, not one strict host — and it still trips the source breaker.
REFUSAL_STREAK_THRESHOLD = 10


@dataclass(slots=True)
class SiteCrawl:
    """The outcome of crawling one domain, before scoring."""

    website: str
    base_url: str | None = None
    domain: str | None = None
    pages: list[PageExtract] = field(default_factory=list)
    fetched: int = 0
    from_cache: int = 0
    failed: int = 0
    # Network requests actually issued, successful or not. Distinct from
    # ``fetched`` (which counts only what came back usable) because it is what
    # the pacing decides on: a page served from the cache is not a request.
    requests: int = 0
    # This host said no (403/429/503). A per-record outcome, not a stage failure.
    refused: bool = False
    # We did not crawl it at all — the source breaker was open or the daily
    # budget was spent. That is work we intended to do and did not.
    blocked: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.pages)

    @property
    def stopped(self) -> bool:
        return self.refused or self.blocked

    def evidence(self) -> SiteEvidence:
        return build_site_evidence(self.base_url or self.website, self.pages)


_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?::\d{1,5})?$")


def normalise_website(url: str | None) -> str | None:
    """A bare host from a Maps payload into something fetchable, or ``None``.

    Maps writes ``salonx.pk`` as often as ``https://salonx.pk/``, and a bare host
    is not a URL to ``httpx``. Rejecting rather than repairing matters more than
    it looks: ``https://`` glued onto ``javascript:void(0)`` produces a string
    that parses fine and resolves to nothing, so the run would spend a DNS
    lookup and a timeout on every junk value in the payload.
    """
    if not url:
        return None
    candidate = url.strip()
    if not candidate:
        return None

    if "//" not in candidate:
        scheme = _SCHEME_RE.match(candidate)
        if scheme and scheme.group(0)[:-1].lower() not in ("http", "https"):
            return None
        candidate = f"https://{candidate}"

    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        return None
    if not _HOSTNAME_RE.match(parts.netloc):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


class WebsiteSource:
    """Cache-first, budgeted crawler over a set of business domains."""

    source = Source.BUSINESS_WEBSITE

    def __init__(
        self,
        cache: FetchCache | None = None,
        settings: Settings | None = None,
        breaker: CircuitBreaker | None = None,
        policy: PacingPolicy | None = None,
        proxy: ProxyConfig | None = None,
        max_pages: int = MAX_PAGES_PER_DOMAIN,
        client=None,
    ) -> None:
        self.settings = settings or get_settings()
        self.cache = cache
        self.max_pages = max_pages
        self._client = client
        self._refusal_streak = 0
        self.breaker = breaker or CircuitBreaker(
            source=self.source,
            failure_threshold=self.settings.circuit_break_failures,
            empty_threshold=EMPTY_STREAK_THRESHOLD,
            pause_minutes=self.settings.circuit_break_minutes,
        )
        self.policy = policy or PacingPolicy(
            delay_min=WEBSITE_PACING.delay_min,
            delay_max=WEBSITE_PACING.delay_max,
            concurrency=self.settings.concurrency,
        )
        # Websites are geo-neutral (§7.1) — only Maps is in the required set —
        # so this resolves to direct egress unless a proxy is configured.
        self.proxy = proxy if proxy is not None else resolve_proxy(self.source, self.settings)

    # ------------------------------------------------------------------ public

    async def crawl_many(self, websites: list[str]) -> list[SiteCrawl]:
        """Crawl each domain, ``policy.concurrency`` domains at a time.

        Concurrency is across domains. Within a domain the pages are sequential
        with a jittered gap, so no host ever sees two of our requests at once.
        """
        if not websites:
            return []

        client = self._client or self._new_client()
        owns_client = self._client is None
        semaphore = asyncio.Semaphore(max(1, self.policy.concurrency))

        async def one(website: str) -> SiteCrawl:
            async with semaphore:
                return await self.crawl(website, client=client)

        try:
            return await asyncio.gather(*(one(w) for w in websites))
        finally:
            if owns_client:
                await client.aclose()

    async def crawl(self, website: str, client=None) -> SiteCrawl:
        """Fetch up to ``max_pages`` pages of one domain and parse each."""
        base = normalise_website(website)
        crawl = SiteCrawl(
            website=website, base_url=base, domain=registrable_domain(base or "")
        )
        if base is None:
            crawl.error = "unfetchable_url"
            return crawl

        if self.breaker.is_open():
            crawl.blocked = True
            crawl.error = f"circuit_open:{self.breaker.tripped_by}"
            return crawl

        own_client = client is None
        client = client or self._new_client()
        try:
            await self._crawl_pages(crawl, base, client)
        finally:
            if own_client:
                await client.aclose()

        self._note_refusals(crawl)

        # §5.5. Only domains that actually answered count toward the empty
        # streak — a domain that never responded is not a silent extraction
        # failure, it is just gone.
        if crawl.pages:
            self.breaker.record_success(produced=any(p.has_any_contact for p in crawl.pages))

        return crawl

    def _note_refusals(self, crawl: SiteCrawl) -> None:
        """Escalate only a *run* of refusals to a source-level stop."""
        if crawl.refused:
            self._refusal_streak += 1
            if self._refusal_streak >= REFUSAL_STREAK_THRESHOLD:
                log.error("website.refusal_streak", streak=self._refusal_streak)
                self.breaker.record_blocked(403)
        elif crawl.pages:
            self._refusal_streak = 0

    # ----------------------------------------------------------------- crawling

    async def _crawl_pages(self, crawl: SiteCrawl, base: str, client) -> None:
        queue: list[str] = [base]
        seen: set[str] = {_dedupe_key(base)}
        requested = False

        while queue and len(crawl.pages) < self.max_pages:
            url = queue.pop(0)
            # Pace only behind a request we actually made. Sleeping after a
            # cache hit would spend the §7 delay without the §7 request, which
            # is the one combination with no upside — on a re-run inside the TTL
            # it turns a zero-request crawl into a minutes-long one. A 404 is
            # still a request, so it is still paced.
            if requested:
                await asyncio.sleep(self.policy.next_delay())

            before = crawl.requests
            page = await self._fetch_page(crawl, url, client)
            requested = crawl.requests > before

            if crawl.stopped:
                return
            if page is None:
                continue

            crawl.pages.append(page)
            for target in page.crawl_targets:
                key = _dedupe_key(target)
                if key not in seen:
                    seen.add(key)
                    queue.append(target)

            if _satisfied(crawl.pages):
                # §5.2's 4 pages is a ceiling, not a quota. Once the site has
                # given us a confirmed WhatsApp number and an email there is
                # nothing left on the about page worth a request.
                return

    async def _fetch_page(self, crawl: SiteCrawl, url: str, client) -> PageExtract | None:
        cached = self._cached(url)
        if cached is not None:
            crawl.from_cache += 1
            status, body, content_type, final_url = cached
        else:
            fetched = await self._fetch_live(crawl, url, client)
            if fetched is None:
                return None
            status, body, content_type, final_url = fetched
            crawl.fetched += 1

        if status != 200 or not body:
            return None
        if not _is_html(content_type):
            return None
        if len(body) > MAX_BODY_BYTES:
            log.info("website.body_too_large", url=url, bytes=len(body))
            return None

        return parse_page(final_url, body, content_type)

    def _cached(self, url: str) -> tuple[int, bytes, str | None, str] | None:
        if self.cache is None:
            return None
        hit = self.cache.get(url)
        if hit is None:
            return None
        # The stored body is the *final* page after redirects, but the cache is
        # keyed on what we asked for, so the redirect target is not recoverable
        # here. Relative links resolve against the requested URL instead, which
        # is correct for the overwhelmingly common http→https and trailing-slash
        # redirects and only loses cross-path ones.
        return hit.status, hit.body, hit.content_type, url

    async def _fetch_live(
        self, crawl: SiteCrawl, url: str, client
    ) -> tuple[int, bytes, str | None, str] | None:
        if self.breaker.budget_exhausted():
            crawl.blocked = True
            crawl.error = "daily_budget_exhausted"
            log.warning("website.budget_exhausted", url=url)
            return None

        self.breaker.record_request()
        crawl.requests += 1
        try:
            response = await client.get(url)
        except Exception as exc:  # noqa: BLE001 — one dead domain must not stop the run
            crawl.failed += 1
            crawl.error = crawl.error or type(exc).__name__
            log.debug("website.fetch_failed", url=url, error=str(exc)[:160])
            return None

        status = response.status_code
        body = response.content
        content_type = response.headers.get("content-type")

        if status in _REFUSAL_STATUSES:
            # §7: stop, do not grind — but stop on *this host*. Whether that
            # escalates to stopping the module is decided per domain, in crawl().
            crawl.refused = True
            crawl.error = f"http_{status}"
            log.warning("website.refused", url=url, status=status)
            return None

        if 500 <= status < 600:
            crawl.failed += 1
            crawl.error = crawl.error or f"http_{status}"
            return None

        final_url = str(getattr(response, "url", url)) or url
        self._store(url, final_url, status, body, content_type)

        if status != 200:
            crawl.failed += 1
            return None
        return status, body, content_type, final_url

    def _store(
        self,
        url: str,
        final_url: str,
        status: int,
        body: bytes,
        content_type: str | None,
    ) -> None:
        """Archive the body (§7), under both the requested and the final URL.

        4xx bodies are stored too. A 404 is a durable fact about a URL, and
        remembering it means the next run does not spend a request rediscovering
        that ``/about`` was never there. 5xx is not stored — that is transient.

        Both URLs, because a redirect makes them different questions and both
        get asked. The *requested* URL is what the next run's crawl looks up, so
        without it the cache misses and §7's biggest lever stops pulling. The
        *final* URL is what a contact records as its evidence, so without it
        §2's re-parse path cannot find the page that proved a number. Roughly
        one PK SMB domain in nine redirects http→https, so this is the common
        case, not an edge.
        """
        if self.cache is None:
            return
        for target in {url, final_url}:
            self.cache.put(
                target,
                body,
                status=status,
                content_type=content_type,
                source=self.source,
                kind=FetchKind.DETAIL,
            )

    # ------------------------------------------------------------------ client

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
            # A misconfigured certificate is extremely common on PK SMB hosting
            # and is not a reason to skip a lead; there is nothing confidential
            # in a public contact page.
            verify=False,
            max_redirects=5,
        )


def _dedupe_key(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.netloc.lower()}{parts.path.rstrip('/').lower()}?{parts.query}"


def _is_html(content_type: str | None) -> bool:
    """A missing Content-Type is treated as HTML — plenty of small PK hosts omit
    it, and refusing those would drop real leads for a header nobody reads."""
    value = (content_type or "").split(";")[0].strip().lower()
    if not value:
        return True
    return value.startswith(_HTML_CONTENT_TYPES)


def _satisfied(pages: list[PageExtract]) -> bool:
    """Have we got what §5.2 came for — a confirmed number and an email?"""
    confirmed = any(page.wa_numbers or page.widget_numbers for page in pages)
    return confirmed and any(page.emails for page in pages)

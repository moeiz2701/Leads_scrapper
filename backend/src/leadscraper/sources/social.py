"""Facebook Pages and Instagram profiles, logged out (§6.4, Tier 3).

**Read §6.7 before changing anything here.** §6 was written before anything was
fetched and Phase 8 measured it; the two corrections that shape this module are:

* §6.4's "ONE logged-out page load … no browser" does not work. A logged-out
  ``httpx`` GET of an Instagram profile returns 200 and ~605 KB of JS shell with
  no bio, no link and no phone; a Facebook Page returns **HTTP 400** and a
  1.5 KB error page for every URL variant. Both render correctly in an ordinary
  logged-out browser (20/20 and 12/12), so this module renders. That is a cost
  change, not a policy change — no login, no credential store, no cookie
  injection, no fingerprint work, which is exactly where §6.1 draws the line.
* §6.4 says to read the bio from ``og:description``. On Instagram that is
  "147K Followers, 179 Following, 5,777 Posts" and can never hold a phone. The
  bio is in ``<meta name="description">``.

And the finding that decides what this module prioritises: **Facebook is the
confirmation engine, not Instagram.** 58% of rendered FB Pages carry an
``api.whatsapp.com/send?phone=`` button against 10% of IG profiles, while IG's
strength is inline bio numbers (50%) which are §9.3 *likely* at 0.60 — the same
score 850 of our 898 businesses already carry. So Facebook is harvested first.

**Scoring choice, and it is deliberate.** §9.3 has two overlapping rows: a
``wa.me``/``api.whatsapp.com`` link at 1.00, and "FB Page WhatsApp button / IG
WhatsApp action" at 0.90. On a platform page these are the same artifact, and the
platform-specific row is the more specific one — so every WhatsApp number found
*on* a Meta property is scored ``PLATFORM_BUTTON`` at 0.90, never 1.00. Both are
`confirmed`, so the exported label is identical; the difference is that the
system never claims stronger evidence than it has, and the "evidence only moves
up" rule then lets a business's own site legitimately raise the same number to
1.00 later.

**§6.6's operating rules are enforced here, not assumed:** logged out; one
request per business per run; 30-day cache; 8–20s randomised delays; concurrency
1; 429/503 stops the module for the run and records ``blocked``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field, replace
from urllib.parse import parse_qs, urlsplit

from leadscraper.config import Settings, get_settings
from leadscraper.core.cache import FetchCache, FetchKind
from leadscraper.core.pacing import SOCIAL_PACING, CircuitBreaker
from leadscraper.core.phone import ParsedPhone, extract_phones
from leadscraper.core.textnorm import registrable_domain
from leadscraper.core.webparse import PageExtract, parse_page
from leadscraper.core.whatsapp import extract_wa_numbers
from leadscraper.enums import Source
from leadscraper.logging import get_logger

log = get_logger(__name__)

RENDER_TIMEOUT_MS = 45_000
NETWORK_IDLE_TIMEOUT_MS = 15_000
MAX_BODY_BYTES = 6_000_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# A rendered body is not the same artifact as a fetched one — the fetched
# Instagram body is a JS shell and the fetched Facebook body is an HTTP 400 error
# page, and both are already in the §7 archive under the plain URL from the recon
# spike. Keying renders separately keeps the module from "hitting cache" on a
# body that provably contains nothing.
RENDER_MARKER = "__render"


def render_cache_url(url: str) -> str:
    """The §7 cache key for a *rendered* copy of ``url``."""
    joiner = "&" if urlsplit(url).query else "?"
    return f"{url}{joiner}{RENDER_MARKER}=1"


# ``/profilecard/`` is Instagram's share-card view, not the profile. It renders
# with no bio and no og tags, so it reads exactly like a soft-gated shell —
# measured: the same handle without the suffix renders fully. Tracking params
# were tested alongside it and make no difference, so they are deliberately left
# alone rather than stripped on suspicion.
_IG_PROFILECARD_RE = re.compile(r"/profilecard/?$", re.IGNORECASE)


def canonical_profile_url(url: str, platform: Source) -> str:
    """The URL that actually serves the profile, where they differ."""
    if platform is Source.INSTAGRAM:
        parts = urlsplit(url)
        if _IG_PROFILECARD_RE.search(parts.path):
            path = _IG_PROFILECARD_RE.sub("", parts.path)
            return f"{parts.scheme}://{parts.netloc}{path}"
    return url


# --------------------------------------------------------------------------- #
# Reading a rendered profile
# --------------------------------------------------------------------------- #

# Meta wraps every outbound link through a redirector, on both platforms.
_LINKSHIM_HOSTS = ("l.instagram.com", "l.facebook.com", "lm.facebook.com")
_LINKSHIM_RE = re.compile(
    r"https?://(?:l\.instagram\.com|l\.facebook\.com|lm\.facebook\.com)/[^\"'\s<>\\]+"
)
_IG_EXTERNAL_URL_RE = re.compile(r'"external_url"\s*:\s*"([^"]+)"')

_JSON_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def unescape_json_slashes(html: str) -> str:
    """Undo the two JSON escapes rendered Meta markup hides its URLs behind.

    Both were found by measurement, and both are the difference between reading
    a page correctly and reading it as empty:

    * ``\\/`` for a slash. Without undoing it, a Facebook Page that plainly
      carries two ``api.whatsapp.com`` links scores as carrying zero, because
      ``core/whatsapp.py``'s regex — correctly — wants real slashes. Undoing the
      escape here is better than loosening a regex §5.2 also depends on.
    * ``\\uXXXX`` for anything else, and Facebook **double-encodes** its outbound
      link shim: the destination arrives as
      ``l.facebook.com/l.php?u=https\\u00253A\\u00252F\\u00252Fwww.example.pk``,
      i.e. a JSON escape wrapping a percent-encoding. A URL matcher stops dead at
      the backslash, so every bio link on the first live run truncated to the
      five characters ``https`` — which reads exactly like "this page has no bio
      link" and is why the run reported 0 websites and 0 socials filled.

    Applied to the whole document rather than to located substrings: we are
    reading a page, not executing it, and a partial decode is what produced the
    bug in the first place.
    """
    decoded = html.replace("\\/", "/")

    def _char(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 16))
        except ValueError:  # pragma: no cover — the regex already guarantees hex
            return match.group(0)

    return _JSON_UNICODE_ESCAPE_RE.sub(_char, decoded)


@dataclass(frozen=True, slots=True)
class SocialTarget:
    """One business's profile on one platform."""

    business_id: object
    name: str
    platform: Source
    url: str


@dataclass(slots=True)
class ProfileRead:
    """What one rendered profile yielded. Every field degrades to empty."""

    target: SocialTarget
    status: int = 0
    from_cache: bool = False
    rendered: bool = False
    blocked: bool = False
    error: str | None = None
    page_name: str | None = None
    # Kept only so the distinction stays visible and testable. §6.4 tells you to
    # read the bio out of this, and on Instagram it is "12K Followers, 10
    # Following, 228 Posts" — a string that can never contain a phone number.
    og_description: str | None = None
    bio_text: str | None = None
    bio_link: str | None = None
    # Numbers carried by a WhatsApp button/link on the platform page. §9.3 0.90.
    button_numbers: tuple[str, ...] = ()
    # Numbers printed in the bio text. §9.3 0.60 unless "WhatsApp" is next to one.
    bio_phones: tuple[ParsedPhone, ...] = ()
    page: PageExtract | None = None

    @property
    def has_findings(self) -> bool:
        return bool(self.button_numbers or self.bio_phones)


@dataclass(slots=True)
class SocialHarvest:
    """Everything one Stage 3 pass fetched."""

    reads: list[ProfileRead] = field(default_factory=list)
    requests: int = 0
    from_cache: int = 0
    refused: bool = False
    blocked: bool = False
    error: str | None = None

    @property
    def stopped(self) -> bool:
        return self.refused or self.blocked


def _meta(tree, name: str) -> str | None:
    for node in tree.css(f'meta[property="{name}"], meta[name="{name}"]'):
        content = (node.attributes.get("content") or "").strip()
        if content:
            return content
    return None


def unshim(url: str) -> str:
    """``l.facebook.com/l.php?u=<encoded>`` → the real destination.

    ``parse_qs`` already percent-decodes once, which is exactly the one layer
    that is always present. A second ``unquote`` is *not* applied: it would
    corrupt any destination legitimately containing ``%25``, and the extra layer
    Facebook adds is a JSON ``\\uXXXX`` escape, which ``unescape_json_slashes``
    has already removed by the time this runs.
    """
    parts = urlsplit(url)
    if parts.netloc.lower() in _LINKSHIM_HOSTS:
        target = parse_qs(parts.query).get("u")
        if target and target[0].strip():
            return target[0].strip()
    return url


def read_profile(target: SocialTarget, body: bytes | str, status: int = 200) -> ProfileRead:
    """One rendered profile → its findings. Pure: no network, no database.

    Written so a markup reshuffle produces an empty read that the §5.5 check
    catches, never a traceback that kills the stage — the §5.1 lesson, applied
    to a third source.
    """
    from selectolax.parser import HTMLParser

    from leadscraper.core.webparse import decode_body

    html = decode_body(body) if isinstance(body, bytes) else body
    html = unescape_json_slashes(html)
    read = ProfileRead(target=target, status=status)

    tree = HTMLParser(html)
    read.page_name = _meta(tree, "og:title")
    # §6.7: the bio is `description`, not `og:description`.
    read.og_description = _meta(tree, "og:description")
    read.bio_text = _meta(tree, "description")
    read.rendered = bool(read.page_name and read.bio_text)

    read.button_numbers = tuple(extract_wa_numbers(html))
    if read.bio_text:
        read.bio_phones = tuple(extract_phones(read.bio_text))
    read.bio_link = _bio_link(html, target.platform)

    # The rest of the page still goes through §5.2's parser rather than a second
    # bespoke one: a rendered profile carries `tel:` hrefs and JSON-LD like any
    # other page, and `parse_page` already reads them.
    with contextlib.suppress(Exception):
        read.page = parse_page(target.url, html)

    return read


def _bio_link(html: str, platform: Source) -> str | None:
    if platform is Source.INSTAGRAM:
        match = _IG_EXTERNAL_URL_RE.search(html)
        if match and match.group(1).strip():
            candidate = unshim(match.group(1).strip())
            if _is_usable_link(candidate):
                return candidate
    shim = _LINKSHIM_RE.search(html)
    if shim:
        target = unshim(shim.group(0).replace("&amp;", "&"))
        if _is_usable_link(target):
            return target
    return None


def _is_usable_link(url: str) -> bool:
    """A destination we would be willing to write into a business row.

    Deliberately paranoid, because the failure this guards against already
    happened: an escaping change truncated every Facebook bio link to the string
    ``"https"``, which ``normalise_website`` would happily turn into the URL
    ``https://https/`` and store as a business's website. A gap-fill that writes
    a nonsense domain is worse than one that writes nothing.
    """
    domain = registrable_domain(url) or ""
    return bool(domain) and "." in domain and domain not in _LINKSHIM_HOSTS


# --------------------------------------------------------------------------- #
# The source
# --------------------------------------------------------------------------- #


class SocialSource:
    """Cache-first, browser-rendered, §6.6-paced reader of Meta profile pages.

    ``renderer`` is injectable so the merge rules and the §6.6 caps can be tested
    against real markup without launching Chromium. The default renderer is the
    only part of this class that needs a browser.
    """

    def __init__(
        self,
        cache: FetchCache | None = None,
        settings: Settings | None = None,
        breakers: dict[str, CircuitBreaker] | None = None,
        policy=None,
        renderer=None,
        on_cached=None,
    ) -> None:
        self.settings = settings or get_settings()
        self.cache = cache
        self.policy = policy or SOCIAL_PACING
        self._renderer = renderer
        # Called after each body is written to the §7 archive. The stage passes
        # ``session.commit`` here, and it is not an optimisation: §6.6's pacing
        # makes a 140-business slice a ~45-minute pass, and without this the
        # whole thing is one transaction — so a crash at minute 44 throws away
        # every rendered body and the re-run pays for all of them again. §7
        # calls the cache the single biggest lever; a lever that only engages if
        # nothing goes wrong is not one. Contacts are applied after the harvest
        # returns, so committing here persists bodies and nothing else.
        self._on_cached = on_cached
        # One breaker per platform. Each is a single host, so §7's per-source
        # rule applies literally — the §5.2 host-vs-source exception is for
        # modules that fan out over hundreds of unrelated hosts, and this is the
        # opposite of that. Facebook refusing us says nothing about Instagram.
        self.breakers = breakers or {
            Source.FACEBOOK: CircuitBreaker(
                source=Source.FACEBOOK,
                failure_threshold=self.settings.circuit_break_failures,
                pause_minutes=self.settings.circuit_break_minutes,
            ),
            Source.INSTAGRAM: CircuitBreaker(
                source=Source.INSTAGRAM,
                failure_threshold=self.settings.circuit_break_failures,
                pause_minutes=self.settings.circuit_break_minutes,
            ),
        }

    async def harvest(self, targets: list[SocialTarget]) -> SocialHarvest:
        """Render every target once, honouring §6.6's caps.

        Targets arrive already ordered by the caller — Facebook first, per §6.7's
        measurement — and the §6.6 one-request-per-business cap is enforced here
        rather than trusted to the caller, because a caller that passes both
        platforms for one business is the easy mistake to make.
        """
        harvest = SocialHarvest()
        if not targets:
            return harvest

        # One *read* per URL, applied to every business that names it — the §5.2
        # rule ("one crawl per domain, not per business") for the same reason:
        # chains and franchises share a Page. Skipping the duplicate URL instead
        # would silently give the second branch nothing, which is §5.5's failure
        # mode wearing a politeness argument. Measured on Lahore × salon: 21
        # Facebook URLs across 21 businesses are 17 distinct Pages.
        by_url: dict[str, ProfileRead] = {}
        budget = max(1, self.settings.social_requests_per_business)

        renderer = self._renderer
        closer = None
        if renderer is None:
            renderer, closer = await self._playwright_renderer()

        try:
            per_business: dict[object, int] = {}
            for target in targets:
                # Only a *refusal* ends the module — §6.6 is explicit that a
                # 429/503 stops it for the run. A breaker merely being open on
                # one platform is §7's case instead ("continue the run with the
                # remaining sources"), and the two are different facts: Facebook
                # refusing us says nothing about Instagram, which is a different
                # host that has not refused anything.
                if harvest.refused:
                    break
                if per_business.get(target.business_id, 0) >= budget:
                    continue

                shared = by_url.get(target.url)
                if shared is not None:
                    # Same Page, different business. Re-point the read rather
                    # than re-render it: no request, no delay, and the second
                    # branch of a chain gets the evidence the first one found.
                    read = replace(shared, target=target, from_cache=True)
                else:
                    read = await self._one(harvest, target, renderer)
                    if read is None:
                        continue
                    by_url[target.url] = read

                per_business[target.business_id] = per_business.get(target.business_id, 0) + 1
                harvest.reads.append(read)
        finally:
            if closer is not None:
                await closer()

        return harvest

    async def _one(self, harvest: SocialHarvest, target: SocialTarget, renderer):
        fetch_url = canonical_profile_url(target.url, target.platform)
        key = render_cache_url(fetch_url)
        cached = self.cache.get(key) if self.cache is not None else None
        if cached is not None:
            harvest.from_cache += 1
            if cached.status != 200 or not cached.body:
                return ProfileRead(target=target, status=cached.status, from_cache=True)
            read = read_profile(target, cached.body, cached.status)
            read.from_cache = True
            return read

        breaker = self.breakers[target.platform]
        if breaker.is_open():
            harvest.blocked = True
            harvest.error = harvest.error or f"circuit_open:{breaker.tripped_by}"
            log.warning("social.circuit_open", url=target.url, platform=str(target.platform))
            # §6.6: a `blocked` record is a valid outcome, not a failure to route
            # around. It is recorded, not silently dropped.
            return ProfileRead(target=target, blocked=True, error="circuit_open")
        if breaker.budget_exhausted():
            harvest.blocked = True
            harvest.error = harvest.error or "daily_budget_exhausted"
            return ProfileRead(target=target, blocked=True, error="budget")

        # §6.6's 8–20s, spent only after a request that was actually made. The
        # §5.3 lesson: sleeping on a cache hit turns a zero-request re-run into a
        # very long one.
        if harvest.requests > 0:
            await asyncio.sleep(self.policy.next_delay())

        breaker.record_request()
        harvest.requests += 1
        html, status = await renderer(fetch_url)
        body = html.encode("utf-8", errors="replace")[:MAX_BODY_BYTES]

        if status in (429, 503):
            # §6.6: honour it, then stop the module for the run. Not a backoff
            # loop — grinding against a refusal is the thing the rule forbids.
            harvest.refused = True
            harvest.error = f"http_{status}"
            breaker.record_blocked(status)
            log.warning("social.refused", url=target.url, status=status)
            return ProfileRead(target=target, status=status, blocked=True, error=f"http_{status}")

        if status != 200 or not body:
            breaker.record_failure(f"http_{status}")
            return ProfileRead(target=target, status=status, error=f"http_{status}")

        read = read_profile(target, body, status)

        # Cache the body only if it is actually a profile. A 200 carrying just
        # the application shell is not a page, and §6.6's 30-day TTL would turn
        # one transient soft-gate into a month of permanent misses: the re-run
        # would "hit cache", find nothing, and report the business as having no
        # bio for thirty days. Measured on Lahore × food — 45 of 140 profiles
        # came back as shells, and re-requesting one later rendered it fine.
        #
        # This is the one place the §7 "save every raw response" rule is
        # deliberately not applied, and the reason is §7's own: the point of
        # keeping bodies is to re-parse them when a selector breaks, and there
        # is nothing in a shell to re-parse.
        if self.cache is not None and read.rendered:
            self.cache.put(
                key,
                body,
                status=status,
                content_type="text/html",
                source=str(target.platform),
                # §6.6 says cache 30 days, which is FetchKind.DETAIL's TTL. A
                # profile is a detail page in every sense that matters here.
                kind=FetchKind.DETAIL,
            )
            if self._on_cached is not None:
                self._on_cached()
        # A 200 that came back is a success, full stop — and this is a measured
        # correction, not a preference.
        #
        # §5.5's empty-streak rule exists to catch "the selectors stopped
        # matching", and ``CircuitBreaker`` implements it as 5 consecutive
        # *unproductive* successes. Passing ``read.rendered`` here made "this
        # particular Page does not expose og tags" count as unproductive, and on
        # the live Lahore × food run that tripped the Facebook breaker 77
        # profiles in and blocked the remaining 29 Pages. The measurement says
        # plainly that it was wrong to: **all 77 Facebook renders returned HTTP
        # 200**, Facebook refused nothing, and 11 of the 77 simply render without
        # og tags — 5 of them happened to land consecutively.
        #
        # 14% of Pages behaving that way is a property of Facebook's population,
        # not a signal about our parser, and a 5-in-a-row run of it is ordinary
        # luck. §5.5's real check therefore lives one level up, at the stage
        # (``YIELD_FLOOR_PROFILES`` in ``services/social.py``), where "the whole
        # stage rendered 15+ profiles and found nothing" *is* diagnostic.
        # ``sources/businesslist.py`` moved the same check up a level for the
        # same reason: an empty category there is a normal outcome too.
        breaker.record_success(produced=True)
        return read

    async def _playwright_renderer(self):
        """A real, logged-out Chromium. Vanilla by policy (§6.1) — see the note.

        No persistent profile, no stored cookies, no stealth patching: a fresh
        context per profile, which is both the least stateful thing to do and the
        thing §6.6 means by "logged out only, no session pool".
        """
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)

        async def render(url: str) -> tuple[str, int]:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                locale="en-PK",
                viewport={"width": 1366, "height": 900},
            )
            page = await context.new_page()
            try:
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS
                )
                status = response.status if response else 0
                # Wait on the network, not on a selector. A selector wait turns
                # "the profile did not load" into a timeout that reads like a
                # transient failure, and §5.5 is emphatic that those are
                # different facts.
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state(
                        "networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS
                    )
                return await page.content(), status
            except Exception as exc:  # noqa: BLE001 — a refusal is a finding
                log.warning("social.render_failed", url=url, error=str(exc)[:160])
                return "", 0
            finally:
                with contextlib.suppress(Exception):
                    await context.close()

        async def close() -> None:
            with contextlib.suppress(Exception):
                await browser.close()
            with contextlib.suppress(Exception):
                await pw.stop()

        return render, close

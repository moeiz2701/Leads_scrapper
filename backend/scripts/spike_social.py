"""Recon spike — is §6's Tier 3 chain actually reachable? (Phase 8)

§6 is the least measured section in implementation.md: it carries no correction
notes at all, and its yield figures ("20–35% of pages surface a number",
"this tier alone should deliver the bulk of your fashion/ecommerce numbers") are
assertions written before anything was fetched. §5.3's prose read the same way
and Phase 6 measured it at zero. So nothing here is assumed.

The load-bearing assumption of the whole tier is one sentence in §6.4:

    IG profile (ONE logged-out page load)
       └→ og:description  → bio text, often contains "03XX..." inline
       └→ bio link URL

Every word of that is a measurable claim, and this script measures them against
real IG and FB URLs already in the database:

  ``--profiles``   Does a logged-out fetch return ``og:description`` in the
                   *served* HTML, or a login wall? Is there a bio link in the
                   markup at all? Does the description carry an inline ``03xx``?
  ``--bio-links``  Follow whatever bio links --profiles found and classify the
                   destinations: wa.me / link-in-bio hub / store / nothing.
                   The distribution is what decides whether §6.4's three-way
                   branch is real or is one branch with two decorations.
  ``--serper``     Tier 2 as a *feeder*: does a targeted
                   ``site:instagram.com "<name>" <city>`` return the profile of
                   the business we searched for? Measured as a join rate against
                   the name we already hold, because Phase 6's whole lesson is
                   that an unjoinable record is worth nothing.
  ``--render``     The follow-up --profiles forces. §6.4 promises the bio link
                   "with no browser"; if the served HTML does not carry it, the
                   next question is whether a *rendered* page does. Rendering a
                   public page the way a browser renders it is not §6.1
                   circumvention — no login, no credential store, no fingerprint
                   work — but it costs a browser per business, which is a
                   different economic claim from the one §6.4 makes.
  ``--ua-probe``   Diagnostic only. If a browser UA gets a wall, does a link-
                   preview crawler UA get og tags? This is a §6.1 *question for
                   the operator*, not a technique to adopt — see the note on
                   ``CRAWLER_UA`` below.

Everything goes through ``core/cache.py`` (§7), so the measurement is
re-runnable at zero request cost and a second opinion never costs a fetch.
§6.6's pacing is honoured even in recon: 8–20s, concurrency 1, stop on 429/503.

Usage:
    PYTHONIOENCODING=utf-8 uv run python scripts/spike_social.py --profiles
    PYTHONIOENCODING=utf-8 uv run python scripts/spike_social.py --bio-links
    PYTHONIOENCODING=utf-8 uv run python scripts/spike_social.py --serper
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

from sqlalchemy import text as sql_text

from leadscraper.config import get_settings
from leadscraper.core.cache import FetchCache, FetchKind
from leadscraper.core.pacing import SOCIAL_PACING
from leadscraper.core.phone import extract_phones
from leadscraper.core.textnorm import normalise_name, registrable_domain
from leadscraper.db.session import session_scope
from leadscraper.sources.social import render_cache_url

# A current desktop Chrome string — the same one §5.3's spike and the website
# module send. This is *not* disguise: it is the UA every ordinary visitor
# sends, and sending a blank or library-default UA is what actually looks
# anomalous. §6.1's line is about not automating past an access control, and
# nothing here does.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Diagnostic only, and deliberately never the default. Meta serves og tags to
# link-preview crawlers so that a pasted URL renders a card. Whether claiming to
# be one of those crawlers is "public data" or "presenting a false identity to
# get different content" is a §6.1 judgement call, and it is the operator's to
# make, not this script's. --ua-probe measures the difference and reports it;
# nothing in the Phase 8 module may use it without that decision being taken.
CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"

REQUEST_TIMEOUT = 25.0
MAX_BODY_BYTES = 8_000_000

# --------------------------------------------------------------------------- #
# Signals in the served HTML
# --------------------------------------------------------------------------- #

# `og:*` is read with selectolax, not a regex, and that is not a style choice.
# The obvious `<meta ... content="(.*?)"` fallback pattern backtracks across the
# whole document from every `<meta` position, and an IG profile is 600 KB of
# markup — it hung this script for minutes before it was replaced. selectolax is
# already a dependency (it is what §5.2 parses with) and does it in one pass.

# The wall, in each platform's own words. Kept as separate alternatives rather
# than one blur so the report can say *which* wall answered.
_IG_WALL_RE = re.compile(
    r"accounts/login|loginForm|Log in to Instagram|LoginAndSignupPage|"
    r"Sorry, this page isn't available",
    re.IGNORECASE,
)
_FB_WALL_RE = re.compile(
    r"You must log ?in to continue|login\.php\?next|checkpoint/|"
    r"content isn.t available right now",
    re.IGNORECASE,
)

# IG keeps the bio link in JSON embedded in the document, not in a href. Both
# shapes are looked for: the legacy scalar and the newer multi-link array.
_IG_EXTERNAL_URL_RE = re.compile(r'"external_url"\s*:\s*"([^"]+)"')
_IG_BIO_LINKS_RE = re.compile(r'"bio_links"\s*:\s*(\[[^\]]*\])')
# Both platforms wrap outbound links through a redirector.
_LINKSHIM_RE = re.compile(
    r"https?://(?:l\.instagram\.com|l\.facebook\.com|lm\.facebook\.com)/[^\"'\s<>\\]+"
)
# ``\/`` because the rendered DOM carries these inside JSON string literals as
# often as in hrefs, and requiring a bare slash silently scored a page that
# plainly contains ``wa.me`` twice as containing it zero times.
_WA_RE = re.compile(r"(?:https?:\\?/\\?/)?(?:api\.whatsapp\.com|wa\.me|whatsapp\.com)\\?/\S*")

# §6.4's named link-in-bio hubs, plus the ones that turned up alongside them.
BIO_HUBS = (
    "linktr.ee", "beacons.ai", "bio.link", "taplink.cc", "campsite.bio",
    "linkin.bio", "milkshake.app", "lnk.bio", "solo.to", "carrd.co",
    "allmylinks.com", "linkpop.com", "shorby.com", "komi.io", "znap.link",
)
# Ordering platforms and delivery aggregators are neither a store we can crawl
# for the business's own number nor a dead end — they are a third thing, and
# lumping them into "store" would overstate §5.2's reach.
AGGREGATORS = (
    "foodpanda.pk", "foodpanda.com", "cheetay.pk", "daraz.pk", "airlift.pk",
    "eatmubarak.pk", "opentable.com", "zomato.com", "tripadvisor.com",
)
SOCIAL_HOSTS = (
    "instagram.com", "facebook.com", "youtube.com", "tiktok.com", "twitter.com",
    "x.com", "threads.net", "snapchat.com", "linkedin.com", "pinterest.com",
)


@dataclass(slots=True)
class ProfileProbe:
    """One logged-out profile fetch, reduced to the facts §6.4 depends on."""

    business: str
    city: str | None
    platform: str
    url: str
    status: int = 0
    bytes: int = 0
    from_cache: bool = False
    og_description: str | None = None
    og_title: str | None = None
    # Kept separate from ``og_description`` because on Instagram they are two
    # different things and only one of them is useful. ``og:description`` is
    # "147K Followers, 179 Following, 5,777 Posts"; ``<meta name=description>``
    # is the actual bio text, which is where §6.4's inline ``03xx`` lives. Read
    # the wrong one and the tier measures as dead when it is not.
    bio_text: str | None = None
    walled: bool = False
    profile_rendered: bool = False
    bio_link: str | None = None
    inline_mobiles: tuple[str, ...] = ()
    inline_landlines: tuple[str, ...] = ()
    wa_in_html: tuple[str, ...] = ()
    error: str | None = None

    @property
    def served_og(self) -> bool:
        return bool(self.og_description)


@dataclass(slots=True)
class SpikeStats:
    requests: int = 0
    from_cache: int = 0
    stopped: bool = False
    stop_reason: str | None = None
    statuses: dict[int, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #


def _cache_url(url: str, variant: str | None) -> str:
    """Cache key for a fetch variant, so the probes never overwrite each other.

    ``normalise_url`` drops fragments and keeps unknown query params, so a
    marker param is the one shape that survives it. The *fetched* URL is always
    the real one; only the key carries the marker.

    Renders deliberately share ``sources/social.py``'s key rather than inventing
    a spike-local one: a rendered body is a rendered body, and the module should
    get the spike's 32 pages for free instead of paying for them twice.
    """
    if not variant:
        return url
    if variant == "render":
        return render_cache_url(url)
    joiner = "&" if urlsplit(url).query else "?"
    return f"{url}{joiner}__spike_ua={variant}"


async def fetch(
    client,
    cache: FetchCache,
    stats: SpikeStats,
    url: str,
    *,
    source: str,
    ua: str = BROWSER_UA,
    variant: str | None = None,
) -> tuple[int, bytes, bool]:
    """Cache-first fetch. Returns (status, body, from_cache).

    §6.6's pacing applies to requests actually made, never to cache hits — the
    §5.3 lesson, where sleeping on a hit turned a zero-request re-run into a
    minutes-long one.
    """
    key = _cache_url(url, variant)
    cached = cache.get(key)
    if cached is not None:
        stats.from_cache += 1
        return cached.status, cached.body, True

    if stats.stopped:
        return 0, b"", False

    if stats.requests > 0:
        await asyncio.sleep(SOCIAL_PACING.next_delay())

    stats.requests += 1
    try:
        response = await client.get(url, headers={"User-Agent": ua})
    except Exception as exc:  # noqa: BLE001 — a dead host is a finding
        return 0, str(exc).encode()[:200], False

    status = response.status_code
    body = response.content[:MAX_BODY_BYTES]
    stats.statuses[status] = stats.statuses.get(status, 0) + 1

    if status in (429, 503):
        # §6.6: honour it, then stop the module for the run. Recon obeys the
        # same rule the module will, or the measurement is of a thing we would
        # never be allowed to build.
        stats.stopped = True
        stats.stop_reason = f"http_{status}"

    cache.put(
        key,
        body,
        status=status,
        content_type=response.headers.get("content-type"),
        source=source,
        # §6.6 says cache 30 days; that is FetchKind.DETAIL's TTL exactly.
        kind=FetchKind.DETAIL,
    )
    return status, body, False


def new_client():
    import httpx

    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-PK,en;q=0.9,ur;q=0.8",
        },
        verify=False,
        max_redirects=5,
    )


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #


def _meta(tree, name: str) -> str | None:
    """One ``<meta>`` value, by ``property`` or ``name``. Unescaping is free."""
    for node in tree.css(f'meta[property="{name}"], meta[name="{name}"]'):
        content = (node.attributes.get("content") or "").strip()
        if content:
            return content
    return None


def _unshim(url: str) -> str:
    """Unwrap ``l.instagram.com/?u=<encoded>`` to the real destination."""
    parts = urlsplit(url)
    if parts.netloc.lower() in ("l.instagram.com", "l.facebook.com", "lm.facebook.com"):
        target = parse_qs(parts.query).get("u")
        if target:
            return unquote(target[0])
    return url


def analyse(probe: ProfileProbe, body: bytes) -> ProfileProbe:
    from selectolax.parser import HTMLParser

    html = body.decode("utf-8", errors="replace")
    probe.bytes = len(body)
    tree = HTMLParser(html)
    probe.og_description = _meta(tree, "og:description")
    probe.bio_text = _meta(tree, "description")
    probe.og_title = _meta(tree, "og:title")

    # "Does the page mention login" and "is the page a wall" are different
    # questions, and conflating them scored ten fully-rendered profiles as
    # walled. A rendered IG profile always carries login links in its chrome.
    # What says the content arrived is the title naming the handle.
    wall = _IG_WALL_RE if probe.platform == "instagram" else _FB_WALL_RE
    probe.walled = bool(wall.search(html))
    # "The content arrived" looks different on each platform: IG stamps the
    # handle into og:title, FB does not. One shared heuristic scored 12 fully
    # rendered FB Pages as empty.
    probe.profile_rendered = bool(
        (probe.og_title and "@" in probe.og_title)
        if probe.platform == "instagram"
        else (probe.og_title and probe.bio_text)
    )

    probe.bio_link = _find_bio_link(html, probe.platform)

    # §6.4: "bio text, often contains 03XX inline". Measured against the bio,
    # which is the only text that claim is about — og:description is a follower
    # count and can never contain a phone number.
    if probe.bio_text:
        found = extract_phones(probe.bio_text)
        probe.inline_mobiles = tuple(p.e164 for p in found if p.line_type == "mobile")
        probe.inline_landlines = tuple(p.e164 for p in found if p.line_type != "mobile")

    probe.wa_in_html = tuple(sorted({m.group(0)[:120] for m in _WA_RE.finditer(html)}))[:5]
    return probe


def _find_bio_link(html: str, platform: str) -> str | None:
    """The outbound link, whichever of the four shapes the page uses."""
    if platform == "instagram":
        match = _IG_EXTERNAL_URL_RE.search(html)
        if match:
            candidate = match.group(1).encode().decode("unicode_escape")
            if candidate.strip():
                return _unshim(candidate)
        links = _IG_BIO_LINKS_RE.search(html)
        if links:
            try:
                for item in json.loads(links.group(1).replace("\\/", "/")):
                    if not isinstance(item, dict):
                        continue
                    url = item.get("url") or item.get("lynx_url")
                    if url:
                        return _unshim(url)
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass

    shim = _LINKSHIM_RE.search(html)
    if shim:
        target = _unshim(shim.group(0).replace("&amp;", "&"))
        if not target.startswith(("https://l.", "https://lm.")):
            return target
    return None


def classify_destination(url: str | None) -> str:
    """§6.4's branch, plus the branches §6.4 does not mention."""
    if not url:
        return "none"
    host = (registrable_domain(url) or "").lower()
    lowered = url.lower()
    if "wa.me" in lowered or "api.whatsapp.com" in lowered or host.endswith("whatsapp.com"):
        return "wa.me"
    if any(hub in host for hub in BIO_HUBS):
        return "bio_hub"
    if any(agg in host for agg in AGGREGATORS):
        return "aggregator"
    if any(soc in host for soc in SOCIAL_HOSTS):
        return "social"
    return "store"


# --------------------------------------------------------------------------- #
# Target selection
# --------------------------------------------------------------------------- #


def load_targets(platform: str, limit: int, seed: int, run_id: str | None) -> list[dict]:
    """Real URLs from the seven runs, sampled deterministically.

    Deterministic because a spike whose sample changes between runs cannot be
    re-checked, and the whole point of routing through §7's cache is that the
    second look is free.
    """
    column = {"instagram": "instagram_url", "facebook": "facebook_url"}[platform]
    where = [f"b.{column} is not null"]
    params: dict = {}
    if run_id:
        where.append("b.run_id = :run_id")
        params["run_id"] = run_id

    query = sql_text(
        f"""
        select distinct on (b.{column})
               b.name, b.city, b.{column} as url, b.id::text as business_id
        from businesses b
        where {" and ".join(where)}
        order by b.{column}
        """  # noqa: S608 — column comes from a two-key literal dict, not input
    )
    with session_scope() as session:
        rows = [dict(r._mapping) for r in session.execute(query, params)]

    rows.sort(key=lambda r: r["url"])
    random.Random(seed).shuffle(rows)
    return rows[:limit]


# --------------------------------------------------------------------------- #
# Mode: --profiles
# --------------------------------------------------------------------------- #


async def run_profiles(args) -> int:
    settings = get_settings()
    probes: list[ProfileProbe] = []
    stats = SpikeStats()

    platforms = ["instagram", "facebook"] if args.platform == "both" else [args.platform]
    targets = {p: load_targets(p, args.limit, args.seed, args.run) for p in platforms}

    with session_scope() as session:
        cache = FetchCache(session, settings)
        client = new_client()
        try:
            for platform in platforms:
                source = platform
                for row in targets[platform]:
                    probe = ProfileProbe(
                        business=row["name"],
                        city=row["city"],
                        platform=platform,
                        url=row["url"],
                    )
                    status, body, cached = await fetch(
                        client, cache, stats, row["url"], source=source
                    )
                    probe.status = status
                    probe.from_cache = cached
                    if status == 0:
                        probe.error = body.decode("utf-8", errors="replace")[:80]
                    elif body:
                        analyse(probe, body)
                    probes.append(probe)
                    session.commit()
                    # Progress, flushed: §6.6 pacing makes this a multi-minute
                    # run and a silent pipe looks identical to a hang.
                    print(
                        f"  [{len(probes):>3}] {platform[:2]} {status} "
                        f"{'cache' if cached else 'live '} "
                        f"og={'Y' if probe.served_og else 'n'} "
                        f"bio={'Y' if probe.bio_link else 'n'} {probe.business[:36]}",
                        flush=True,
                    )
                    if stats.stopped:
                        break
                if stats.stopped:
                    break
        finally:
            await client.aclose()

    _report_profiles(probes, stats)
    _write_bio_links(probes, args.out)
    return 0


def _report_profiles(probes: list[ProfileProbe], stats: SpikeStats) -> None:
    print(f"\n{'=' * 78}\n§6 TIER 3 RECON — logged-out profile fetch\n{'=' * 78}")
    print(
        f"requests={stats.requests}  from_cache={stats.from_cache}  "
        f"statuses={dict(sorted(stats.statuses.items()))}"
        + (f"  STOPPED: {stats.stop_reason}" if stats.stopped else "")
    )

    for platform in ("instagram", "facebook"):
        group = [p for p in probes if p.platform == platform]
        if not group:
            continue
        n = len(group)
        og = sum(1 for p in group if p.served_og)
        rendered = sum(1 for p in group if p.profile_rendered)
        walled = sum(1 for p in group if p.walled)
        biotext = sum(1 for p in group if p.bio_text)
        bio = sum(1 for p in group if p.bio_link)
        inline = sum(1 for p in group if p.inline_mobiles)
        inline_any = sum(1 for p in group if p.inline_mobiles or p.inline_landlines)
        wa = sum(1 for p in group if p.wa_in_html)
        ok = sum(1 for p in group if p.status == 200)
        actionable = sum(
            1 for p in group if p.inline_mobiles or classify_destination(p.bio_link) == "wa.me"
        )
        print(f"\n--- {platform}  (n={n}) ---")
        print(f"  HTTP 200                    {ok:>3} / {n}")
        print(f"  profile content rendered    {rendered:>3} / {n}")
        print(f"  og:description served       {og:>3} / {n}   (follower counts, not the bio)")
        print(f"  bio text served             {biotext:>3} / {n}   <- §6.4's assumption")
        print(f"  login markup present        {walled:>3} / {n}   (chrome, not a wall)")
        print(f"  bio link in the DOM         {bio:>3} / {n}")
        print(f"  inline 03xx MOBILE in bio   {inline:>3} / {n}   <- §6.4 'often'")
        print(f"  inline any phone in bio     {inline_any:>3} / {n}")
        print(f"  any wa.me anywhere in HTML  {wa:>3} / {n}")
        print(f"  ** a number, one page-load  {actionable:>3} / {n} **")

        dist: dict[str, int] = {}
        for p in group:
            dest = classify_destination(p.bio_link)
            dist[dest] = dist.get(dest, 0) + 1
        print(f"  bio-link destinations: {dict(sorted(dist.items(), key=lambda kv: -kv[1]))}")

    print(f"\n{'-' * 78}\nper-URL detail\n{'-' * 78}")
    for p in probes:
        flags = []
        if p.walled:
            flags.append("WALL")
        if p.served_og:
            flags.append("OG")
        if p.bio_link:
            flags.append(f"BIO:{classify_destination(p.bio_link)}")
        if p.inline_mobiles:
            flags.append(f"PHONE:{len(p.inline_mobiles)}")
        if p.from_cache:
            flags.append("cached")
        if p.error:
            flags.append(p.error)
        print(f"  {p.status:>3} {p.bytes:>8} {p.business[:28]:28} {' '.join(flags)}")
        if p.bio_text:
            print(f"       bio: {p.bio_text[:220]}")
        if p.bio_link:
            print(f"      link: {p.bio_link[:110]}")
        if p.inline_mobiles:
            print(f"    MOBILE: {list(p.inline_mobiles)}")
        if p.wa_in_html:
            print(f"     wa.me: {list(p.wa_in_html)[:2]}")


def _write_bio_links(probes: list[ProfileProbe], out: str) -> None:
    from pathlib import Path

    rows = [
        {
            "business": p.business,
            "city": p.city,
            "platform": p.platform,
            "profile": p.url,
            "bio_link": p.bio_link,
            "destination": classify_destination(p.bio_link),
        }
        for p in probes
        if p.bio_link
    ]
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(rows)} bio links -> {path.resolve()}")


# --------------------------------------------------------------------------- #
# Mode: --bio-links
# --------------------------------------------------------------------------- #


async def run_bio_links(args) -> int:
    from pathlib import Path

    from leadscraper.core.webparse import parse_page

    path = Path(args.out)
    if not path.exists():
        print(f"No bio links recorded yet — run --profiles first ({path}).")
        return 1
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not rows:
        print("No bio links to follow. That is the finding.")
        return 0

    settings = get_settings()
    stats = SpikeStats()
    results = []

    with session_scope() as session:
        cache = FetchCache(session, settings)
        client = new_client()
        try:
            for row in rows:
                url = row["bio_link"]
                dest = row["destination"]
                if dest == "wa.me":
                    # Already the answer. §6.4's shortest branch, and it costs
                    # no request — the number is in the URL.
                    results.append({**row, "status": 200, "wa_numbers": _wa_from_url(url)})
                    continue
                status, body, cached = await fetch(
                    client, cache, stats, url, source="bio_link"
                )
                session.commit()
                record = {**row, "status": status, "from_cache": cached}
                if status == 200 and body:
                    # §6.4's linktr.ee branch needs no second parser: a
                    # link-in-bio hub is a page, and parse_page already reads
                    # wa.me hrefs, widgets, tel: and JSON-LD out of any page.
                    extract = parse_page(url, body)
                    record["wa_numbers"] = extract.wa_numbers
                    record["tel_numbers"] = [p.e164 for p in extract.tel_numbers]
                    record["text_phones"] = [p.e164 for p in extract.text_phones][:5]
                    record["widget_numbers"] = extract.widget_numbers
                results.append(record)
                if stats.stopped:
                    break
        finally:
            await client.aclose()

    _report_bio_links(results, stats)
    return 0


def _wa_from_url(url: str) -> list[str]:
    from leadscraper.core.whatsapp import extract_wa_numbers

    return extract_wa_numbers(url)


def _report_bio_links(results: list[dict], stats: SpikeStats) -> None:
    print(f"\n{'=' * 78}\n§6.4 BIO-LINK FOLLOW\n{'=' * 78}")
    print(
        f"requests={stats.requests}  from_cache={stats.from_cache}  "
        f"statuses={dict(sorted(stats.statuses.items()))}"
        + (f"  STOPPED: {stats.stop_reason}" if stats.stopped else "")
    )
    n = len(results)
    with_wa = sum(1 for r in results if r.get("wa_numbers"))
    with_any = sum(
        1
        for r in results
        if r.get("wa_numbers") or r.get("tel_numbers") or r.get("widget_numbers")
    )
    print(f"\n  bio links followed          {n}")
    print(f"  yielded a wa.me number      {with_wa:>3} / {n}   <- the confirmed label")
    print(f"  yielded any number at all   {with_any:>3} / {n}")

    by_dest: dict[str, list[int]] = {}
    for r in results:
        bucket = by_dest.setdefault(r["destination"], [0, 0])
        bucket[0] += 1
        if r.get("wa_numbers"):
            bucket[1] += 1
    print("\n  by destination type:")
    for dest, (total, hits) in sorted(by_dest.items(), key=lambda kv: -kv[1][0]):
        print(f"    {dest:12} {hits:>3} wa.me / {total:>3} followed")

    print(f"\n{'-' * 78}")
    for r in results:
        wa = ",".join(r.get("wa_numbers") or []) or "-"
        print(
            f"  {str(r.get('status', 0)):>3} {r['destination']:10} {r['business'][:26]:26} "
            f"wa={wa[:40]}"
        )


# --------------------------------------------------------------------------- #
# Mode: --serper  (Tier 2 as a feeder, §6.3)
# --------------------------------------------------------------------------- #


async def run_serper(args) -> int:
    """Does a targeted query return the profile of the business we searched for?

    §6.3's published example is a *broad* query. Phase 6's lesson says a broad
    result has no place_id, no coordinates and therefore nothing to join to, so
    what is measured here instead is the targeted form — one query naming one
    business we already hold — where the join is true by construction *if* the
    top hit is really that business. That "if" is the whole measurement.
    """
    from rapidfuzz.fuzz import token_set_ratio

    settings = get_settings()
    if not settings.serp_api_key:
        print("SERP_API_KEY is not set. Nothing to measure.")
        return 1
    if settings.serp_provider != "serper":
        print(f"Only the serper provider is wired here; SERP_PROVIDER={settings.serp_provider}")
        return 1

    # Two populations, and both are needed to read the result.
    #
    # ``--control`` takes businesses whose IG URL we ALREADY hold, so the top
    # hit can be checked against ground truth rather than against a name ratio
    # that is itself unvalidated. Without it, a high name ratio could mean
    # "found the right profile" or "found a differently-owned page with a
    # similar name", and those are opposite outcomes.
    #
    # The default takes the population Tier 2 exists for: the businesses with no
    # social URL at all, where a hit is new information.
    predicate = (
        "b.instagram_url is not null"
        if args.control
        else "b.instagram_url is null and b.facebook_url is null"
    )
    with session_scope() as session:
        rows = [
            dict(r._mapping)
            for r in session.execute(
                sql_text(
                    f"""
                    select b.name, b.city, b.id::text as business_id,
                           b.instagram_url as known_url
                    from businesses b
                    where {predicate} and b.city is not null
                    order by b.name
                    """  # noqa: S608 — predicate is one of two literals above
                )
            )
        ]
    rows.sort(key=lambda r: (r["name"], r["business_id"]))
    random.Random(args.seed).shuffle(rows)
    targets = rows[: args.limit]

    stats = SpikeStats()
    results = []

    import httpx

    with session_scope() as session:
        cache = FetchCache(session, settings)
        async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT)) as client:
            for row in targets:
                query = f'site:instagram.com "{row["name"]}" {row["city"]}'
                # Cached under a synthetic URL so a re-run costs no credits.
                key = f"https://serper.dev/search?q={query}"
                cached = cache.get(key)
                if cached is not None:
                    stats.from_cache += 1
                    payload = json.loads(cached.text() or "{}")
                else:
                    if stats.requests > 0:
                        await asyncio.sleep(1.0)
                    stats.requests += 1
                    response = await client.post(
                        "https://google.serper.dev/search",
                        headers={
                            "X-API-KEY": settings.serp_api_key,
                            "Content-Type": "application/json",
                        },
                        json={"q": query, "gl": "pk", "num": 10},
                    )
                    stats.statuses[response.status_code] = (
                        stats.statuses.get(response.status_code, 0) + 1
                    )
                    if response.status_code != 200:
                        stats.stopped = True
                        stats.stop_reason = f"http_{response.status_code}"
                        break
                    payload = response.json()
                    cache.put(
                        key,
                        response.content,
                        status=200,
                        content_type="application/json",
                        source="serp",
                        kind=FetchKind.DETAIL,
                    )
                    session.commit()

                organic = payload.get("organic") or []
                top = organic[0] if organic else None
                record = {
                    "business": row["name"],
                    "city": row["city"],
                    "hits": len(organic),
                    "top_url": (top or {}).get("link"),
                    "top_title": (top or {}).get("title"),
                    "snippet": (top or {}).get("snippet"),
                    "known_url": row.get("known_url"),
                }
                if top and row.get("known_url"):
                    # Ground truth, where we have it: is the profile Serper
                    # returned the same handle we already recorded from the
                    # business's own website? This is the only unambiguous
                    # measure of the join.
                    record["handle_match"] = _handle(top.get("link")) == _handle(row["known_url"])
                    record["known_anywhere"] = any(
                        _handle(h.get("link")) == _handle(row["known_url"]) for h in organic
                    )
                if top:
                    # The join test: does the returned profile's title actually
                    # name the business we asked about? Same normaliser and same
                    # ratio §10.1 uses, so the number is comparable to the
                    # dedupe threshold rather than a new invented scale.
                    record["name_ratio"] = token_set_ratio(
                        normalise_name(row["name"]),
                        normalise_name(re.split(r"[|(]", top.get("title") or "")[0]),
                    )
                    record["snippet_mobiles"] = [
                        p.e164
                        for p in extract_phones(
                            f"{top.get('title', '')} {top.get('snippet', '')}"
                        )
                        if p.line_type == "mobile"
                    ]
                results.append(record)

    _report_serper(results, stats)
    return 0


def _handle(url: str | None) -> str | None:
    """``instagram.com/<handle>`` → ``handle``, lowercased. Ignores /p/ posts."""
    if not url:
        return None
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if not parts or parts[0] in ("p", "reel", "reels", "explore", "tv", "stories"):
        return None
    return parts[0].lower()


def _report_serper(results: list[dict], stats: SpikeStats) -> None:
    print(f"\n{'=' * 78}\n§6.3 TIER 2 AS A FEEDER — targeted site:instagram.com\n{'=' * 78}")
    print(
        f"credits_spent={stats.requests}  from_cache={stats.from_cache}"
        + (f"  STOPPED: {stats.stop_reason}" if stats.stopped else "")
    )
    n = len(results)
    if not n:
        return
    any_hit = sum(1 for r in results if r["hits"])
    for bar in (60, 75, 88):
        joined = sum(1 for r in results if (r.get("name_ratio") or 0) >= bar)
        print(f"  top hit names the business at ratio >= {bar:>3}   {joined:>3} / {n}")
    snip = sum(1 for r in results if r.get("snippet_mobiles"))
    print(f"\n  queries returning any result            {any_hit:>3} / {n}")
    print(f"  snippet carried a mobile                {snip:>3} / {n}   <- §6.3's 20-35%")

    ground = [r for r in results if r.get("known_url")]
    if ground:
        top_match = sum(1 for r in ground if r.get("handle_match"))
        any_match = sum(1 for r in ground if r.get("known_anywhere"))
        print(f"\n  --- ground truth (n={len(ground)}), profile already known ---")
        print(f"  top hit IS the known handle             {top_match:>3} / {len(ground)}")
        print(f"  known handle anywhere in top 10         {any_match:>3} / {len(ground)}")
    print(f"\n{'-' * 78}")
    for r in results:
        print(
            f"  {str(r.get('name_ratio', '-')):>5} {r['business'][:30]:30} "
            f"{(r.get('top_url') or '(no hit)')[:60]}"
        )
        if r.get("snippet_mobiles"):
            print(f"        snippet mobiles: {r['snippet_mobiles']}")


# --------------------------------------------------------------------------- #
# Mode: --render  (does a browser see what a fetcher cannot?)
# --------------------------------------------------------------------------- #


async def run_render(args) -> int:
    """Load each profile in a real, logged-out browser and re-run ``analyse``.

    Deliberately vanilla Playwright: default fingerprint, no stealth plugin, no
    cookie injection, no login. §6.1 rules out disguise, and the point of this
    measurement is what an ordinary browser sees — if that needs disguise to
    work, the answer is that the tier does not work.

    The rendered DOM is cached like any other body (§7), under a marker key, so
    the parse can be re-run later without launching a browser again.
    """
    from playwright.async_api import async_playwright

    settings = get_settings()
    stats = SpikeStats()
    probes: list[ProfileProbe] = []
    platforms = ["instagram", "facebook"] if args.platform == "both" else [args.platform]

    with session_scope() as session:
        cache = FetchCache(session, settings)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                for platform in platforms:
                    for row in load_targets(platform, args.limit, args.seed, args.run):
                        probe = ProfileProbe(
                            business=row["name"], city=row["city"],
                            platform=platform, url=row["url"],
                        )
                        key = _cache_url(row["url"], "render")
                        cached = cache.get(key)
                        if cached is not None:
                            stats.from_cache += 1
                            probe.status, probe.from_cache = cached.status, True
                            analyse(probe, cached.body)
                        else:
                            if stats.requests > 0:
                                await asyncio.sleep(SOCIAL_PACING.next_delay())
                            stats.requests += 1
                            html, status = await _render_one(browser, row["url"])
                            probe.status = status
                            body = html.encode("utf-8", errors="replace")
                            cache.put(
                                key, body, status=status, content_type="text/html",
                                source=platform, kind=FetchKind.DETAIL,
                            )
                            session.commit()
                            analyse(probe, body)
                        probes.append(probe)
                        print(
                            f"  [{len(probes):>3}] {platform[:2]} {probe.status} "
                            f"{'cache' if probe.from_cache else 'live '} "
                            f"og={'Y' if probe.served_og else 'n'} "
                            f"bio={'Y' if probe.bio_link else 'n'} "
                            f"{probe.business[:34]}",
                            flush=True,
                        )
            finally:
                await browser.close()

    _report_profiles(probes, stats)
    _write_bio_links(probes, args.out)
    return 0


async def _render_one(browser, url: str) -> tuple[str, int]:
    """One page, one fresh context. Returns (rendered HTML, status)."""
    context = await browser.new_context(
        user_agent=BROWSER_UA,
        locale="en-PK",
        viewport={"width": 1366, "height": 900},
    )
    page = await context.new_page()
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        status = response.status if response else 0
        # The profile header hydrates after the shell. Waiting on the network
        # rather than a selector keeps this honest about *what* rendered — a
        # selector wait would quietly convert "nothing loaded" into a timeout we
        # might read as a transient failure.
        with contextlib.suppress(Exception):  # a busy page is still a page
            await page.wait_for_load_state("networkidle", timeout=15_000)
        return await page.content(), status
    except Exception as exc:  # noqa: BLE001 — a refusal is a finding
        return f"<!-- render failed: {type(exc).__name__}: {str(exc)[:200]} -->", 0
    finally:
        await context.close()


# --------------------------------------------------------------------------- #
# Mode: --ua-probe  (diagnostic; see the CRAWLER_UA note)
# --------------------------------------------------------------------------- #


async def run_ua_probe(args) -> int:
    settings = get_settings()
    stats = SpikeStats()
    platforms = ["instagram", "facebook"] if args.platform == "both" else [args.platform]

    print(f"\n{'=' * 78}\nUA DIAGNOSTIC — browser UA vs link-preview crawler UA\n{'=' * 78}")
    print(
        "Reported so the §6.1 decision is made on evidence. A difference here is\n"
        "a question for the operator, not a licence: presenting a false identity\n"
        "to obtain content a browser is refused is the thing §6.1 rules out.\n"
    )

    with session_scope() as session:
        cache = FetchCache(session, settings)
        client = new_client()
        try:
            for platform in platforms:
                for row in load_targets(platform, args.limit, args.seed, args.run):
                    line = [f"{platform[:2]} {row['name'][:26]:26}"]
                    for label, ua, variant in (
                        ("browser", BROWSER_UA, None),
                        ("crawler", CRAWLER_UA, "crawler"),
                    ):
                        status, body, _ = await fetch(
                            client, cache, stats, row["url"],
                            source=platform, ua=ua, variant=variant,
                        )
                        probe = ProfileProbe(
                            business=row["name"], city=row["city"],
                            platform=platform, url=row["url"], status=status,
                        )
                        if body:
                            analyse(probe, body)
                        line.append(
                            f"{label}={status} og={'Y' if probe.served_og else 'n'} "
                            f"wall={'Y' if probe.walled else 'n'} "
                            f"bio={'Y' if probe.bio_link else 'n'}"
                        )
                    session.commit()
                    print("  " + "  |  ".join(line))
                    if stats.stopped:
                        break
        finally:
            await client.aclose()

    print(f"\nrequests={stats.requests} from_cache={stats.from_cache} statuses={stats.statuses}")
    return 0


# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--profiles", action="store_true", help="logged-out IG/FB profile fetch")
    mode.add_argument("--bio-links", action="store_true", help="follow what --profiles found")
    mode.add_argument("--serper", action="store_true", help="§6.3 targeted-query join rate")
    mode.add_argument("--render", action="store_true", help="same profiles, in a real browser")
    mode.add_argument("--ua-probe", action="store_true", help="diagnostic: browser vs crawler UA")
    parser.add_argument("--platform", choices=("instagram", "facebook", "both"), default="both")
    parser.add_argument("--limit", type=int, default=20, help="businesses per platform")
    parser.add_argument("--seed", type=int, default=8, help="deterministic sample")
    parser.add_argument("--run", default=None, help="restrict to one run id")
    parser.add_argument("--out", default="./data/spike/social_bio_links.json")
    parser.add_argument(
        "--control",
        action="store_true",
        help="--serper only: query businesses whose IG URL we already know, to "
        "measure the join against ground truth instead of a name ratio",
    )
    args = parser.parse_args()

    if args.profiles:
        return asyncio.run(run_profiles(args))
    if args.bio_links:
        return asyncio.run(run_bio_links(args))
    if args.serper:
        return asyncio.run(run_serper(args))
    if args.render:
        return asyncio.run(run_render(args))
    return asyncio.run(run_ua_probe(args))


if __name__ == "__main__":
    sys.exit(main())

"""§6 Tier 3 — reading a rendered Facebook Page or Instagram profile (Phase 8).

Every markup shape below was taken from a real rendered page during the §6.7
recon, not invented. That matters more here than in most parsers: §6 shipped
with four factual claims about this markup and **three of them were wrong**, so
a test written from the document rather than from the page would pin the wrong
behaviour. The three, each pinned by a test in this file:

* §6.4 says the bio is in ``og:description``. On Instagram that tag holds
  "12K Followers, 10 Following, 228 Posts" and the bio is in
  ``<meta name="description">``.
* §6.4 says a bio link is "virtually always" a Linktree-style hub with a
  WhatsApp button. Across 32 profiles, **zero** were.
* §6.4 promises one plain page load. Rendered React writes these URLs inside
  JSON string literals as ``https:\\/\\/api.whatsapp.com\\/send``, so the
  escaping — not the fetching — is what decides whether a Page carrying two
  WhatsApp links scores as carrying two or as carrying none.

The source-level tests inject a renderer, so none of this needs a browser.
"""

from __future__ import annotations

import asyncio
import gzip
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from leadscraper.core.pacing import CircuitBreaker, PacingPolicy
from leadscraper.db.models import Business, Contact, Run
from leadscraper.enums import (
    BelongsTo,
    ContactKind,
    LineType,
    NumberPreference,
    RunStatus,
    Source,
    WhatsAppLabel,
)
from leadscraper.services.social import (
    BIO_TEXT_CONFIDENCE,
    BUTTON_CONFIDENCE,
    enrich_socials,
    findings_for,
)
from leadscraper.sources.social import (
    ProfileRead,
    SocialSource,
    SocialTarget,
    canonical_profile_url,
    read_profile,
    render_cache_url,
    unescape_json_slashes,
    unshim,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _target(
    platform: Source = Source.FACEBOOK,
    url: str = "https://www.facebook.com/example",
    business_id: object | None = None,
) -> SocialTarget:
    return SocialTarget(
        business_id=business_id or uuid.uuid4(),
        name="Example",
        platform=platform,
        url=url,
    )


# --------------------------------------------------------------------------- #
# §6.7 correction 1 — the bio is not in og:description
# --------------------------------------------------------------------------- #

# Instagram's two description tags, verbatim in shape from @blushbarbyhos.
_IG_HEAD = """
<html><head>
<meta property="og:title" content="Blush Bar by House of Salons (@blushbarbyhos) • Instagram">
<meta property="og:description" content="12K Followers, 10 Following, 228 Posts">
<meta name="description" content="12K Followers, 10 Following, 228 Posts - Blush Bar
by House of Salons (@blushbarbyhos) on Instagram: &quot;Blush Bar by House of Salon
Makeup | Nails | Lashes
Contact No. +923190155026&quot;">
</head><body></body></html>
"""


def test_the_bio_is_read_from_description_not_og_description():
    """§6.4 names the wrong tag, and the wrong tag can never hold a phone.

    ``og:description`` on Instagram is a follower count. Reading it — as §6.4
    instructs — measured this tier at 0 numbers in 20 profiles when the real
    figure is 10 in 20. This is the single most expensive error in §6.
    """
    read = read_profile(_target(Source.INSTAGRAM), _IG_HEAD)

    # The tag §6.4 names, and what it actually holds.
    assert read.og_description == "12K Followers, 10 Following, 228 Posts"
    assert read.bio_text is not None
    assert "Contact No." in read.bio_text
    assert [p.e164 for p in read.bio_phones] == ["+923190155026"]


def test_a_profile_with_only_a_follower_count_yields_no_number():
    """The negative of the same rule: no bio means no number, not a parse error."""
    html = _IG_HEAD.replace('name="description"', 'name="unrelated"')
    read = read_profile(_target(Source.INSTAGRAM), html)

    assert read.bio_text is None
    assert read.bio_phones == ()
    assert not read.has_findings


# --------------------------------------------------------------------------- #
# §6.7 correction 3 — escaped slashes in rendered JSON
# --------------------------------------------------------------------------- #

# Verbatim shape from the rendered Chai Studio Page: the WhatsApp button lives
# in a JSON blob, not in an href, and every slash is escaped.
_FB_WITH_BUTTON = """
<html><head>
<meta property="og:title" content="Chai Studio">
<meta name="description" content="Chai Studio, Lahore. 12,437 likes. Studio of Culture.">
</head><body>
<script type="application/json">{"cta_link":
"https:\\/\\/api.whatsapp.com\\/send?phone=923211220011&source=FB_Post"}</script>
</body></html>
"""


def test_a_whatsapp_button_inside_escaped_json_is_still_a_whatsapp_button():
    """The finding that decides whether Facebook is worth anything at all.

    58% of rendered FB Pages carry this button (§6.7) and it is §9.3's 0.90 row
    — the reason Phase 8 reads Facebook before Instagram. Every one of them is
    written with ``\\/`` escapes, so without ``unescape_json_slashes`` a Page
    that plainly carries a WhatsApp link scores as carrying none.
    """
    assert "//api.whatsapp.com/send" not in _FB_WITH_BUTTON  # escaped in the source

    read = read_profile(_target(Source.FACEBOOK), _FB_WITH_BUTTON)

    assert read.button_numbers == ("+923211220011",)


def test_unescape_leaves_an_ordinary_url_alone():
    plain = '<a href="https://api.whatsapp.com/send?phone=923211220011">chat</a>'
    assert unescape_json_slashes(plain) == plain


def test_a_platform_button_scores_090_not_100():
    """§9.3 has two overlapping rows and this module takes the specific one.

    A ``wa.me`` link on a business's own site is 1.00; the same artifact
    surfaced through a Meta property is "FB Page WhatsApp button" at 0.90. Both
    export as `confirmed`, so nothing the operator sees changes — what changes
    is that the system never claims stronger evidence than it holds, which is
    what lets §5.2 legitimately raise the same number to 1.00 later.
    """
    read = read_profile(_target(Source.FACEBOOK), _FB_WITH_BUTTON)
    findings = findings_for(read)

    assert len(findings) == 1
    assert findings[0].evidence.score == pytest.approx(0.90)
    assert findings[0].evidence.label is WhatsAppLabel.CONFIRMED
    assert findings[0].confidence == BUTTON_CONFIDENCE


def test_a_button_only_number_is_still_classified_as_a_mobile():
    """The number this stage exists to find must not be the one the export drops.

    A WhatsApp button number usually appears nowhere in the bio text, so there is
    no parsed phone to borrow a line type from. Leaving it UNKNOWN is not
    harmless: §10.2 qualifies on "≥60 **and a mobile**" and §3.3's
    `whatsapp_only` ranks on line type, so a confirmed WhatsApp number would
    score 84 and then be filtered out of the CSV. Caught on a live run — two
    Lahore × food rows exported as `confirmed` with type `unknown`.
    """
    read = read_profile(_target(Source.FACEBOOK), _FB_WITH_BUTTON)
    assert read.bio_phones == (), "the number is only in the button, not the bio"

    finding = findings_for(read)[0]
    assert finding.line_type is LineType.MOBILE
    assert finding.operator is not None


def test_a_bio_number_with_no_button_is_likely_not_confirmed():
    """§9.3's 0.60 row. Half of IG bios print a number and it proves nothing
    about WhatsApp — this is the same score 850 of 898 businesses already had."""
    read = read_profile(_target(Source.INSTAGRAM), _IG_HEAD)
    findings = findings_for(read)

    assert [f.e164 for f in findings] == ["+923190155026"]
    assert findings[0].evidence.score == pytest.approx(0.60)
    assert findings[0].evidence.label is WhatsAppLabel.LIKELY
    assert findings[0].confidence == BIO_TEXT_CONFIDENCE


def test_the_word_whatsapp_beside_a_bio_number_lifts_it_to_075():
    """§9.3's proximity row. A bio is short enough that 50 chars is a real
    adjacency test rather than "somewhere on the page"."""
    html = _IG_HEAD.replace("Contact No. +923190155026", "WhatsApp us on +923190155026")
    findings = findings_for(read_profile(_target(Source.INSTAGRAM), html))

    assert findings[0].evidence.score == pytest.approx(0.75)
    assert findings[0].evidence.label is WhatsAppLabel.LIKELY


def test_a_button_and_a_bio_line_for_one_number_produce_one_contact():
    """Evidence does not accumulate (§9.3) and a number is not two contacts."""
    html = _FB_WITH_BUTTON.replace(
        "Studio of Culture.", "Studio of Culture. Call 0321 1220011"
    )
    findings = findings_for(read_profile(_target(Source.FACEBOOK), html))

    assert [f.e164 for f in findings] == ["+923211220011"]
    assert findings[0].evidence.score == pytest.approx(0.90)


# --------------------------------------------------------------------------- #
# Bio links — §6.7 correction 2
# --------------------------------------------------------------------------- #


def test_a_link_shim_is_unwrapped_to_its_destination():
    """Both platforms wrap outbound links; the shim is not the bio link."""
    shimmed = "https://l.facebook.com/l.php?u=https%3A%2F%2Fkarakkhel.com%2F&h=AT2"
    assert unshim(shimmed) == "https://karakkhel.com/"


# Verbatim from the rendered Kashee's Page. Facebook wraps the destination in a
# percent-encoding and then escapes the percent signs as JSON ``%`` — so
# what is actually in the document is ``https%3A%2F%2F…``.
_FB_DOUBLE_ENCODED_SHIM = (
    '<html><head><meta property="og:title" content="Kashees">'
    '<meta name="description" content="Kashees, Lahore. 1 like."></head><body>'
    '<script>{"link":"https:\\/\\/l.facebook.com\\/l.php?u='
    'https\\u00253A\\u00252F\\u00252Fwww.kashees.com\\u00252F&h=AT2"}</script>'
    "</body></html>"
)


def test_facebooks_double_encoded_shim_survives_extraction():
    """The bug the first live run found, pinned so it cannot come back quietly.

    A URL matcher stops at the backslash, so every Facebook bio link truncated to
    the five characters ``https``. That is indistinguishable from "this Page has
    no bio link" in the run stats — the run honestly reported 0 websites and 0
    socials filled and nothing looked broken. §5.5's failure mode exactly, one
    layer down from where §5.5 expects it.
    """
    read = read_profile(_target(Source.FACEBOOK), _FB_DOUBLE_ENCODED_SHIM)

    assert read.bio_link == "https://www.kashees.com/"


def test_a_truncated_bio_link_is_refused_rather_than_stored():
    """Defence in depth for the same bug's blast radius.

    ``normalise_website("https")`` yields ``https://https/``, which would have
    been written into a business row as its website. A gap-fill that stores a
    nonsense domain is worse than one that stores nothing, so the link must
    carry a real domain before it is offered to anyone.
    """
    broken = _FB_DOUBLE_ENCODED_SHIM.replace(
        "https\\u00253A\\u00252F\\u00252Fwww.kashees.com\\u00252F", "https"
    )
    read = read_profile(_target(Source.FACEBOOK), broken)

    assert read.bio_link is None


def test_unicode_escapes_are_decoded_alongside_slashes():
    assert unescape_json_slashes("a\\u00252Fb") == "a%2Fb"
    assert unescape_json_slashes("x\\/y") == "x/y"


def test_an_instagram_external_url_is_the_bio_link():
    html = _IG_HEAD.replace(
        "</head>",
        '<script>{"external_url":"https://blushbarbyhos.com"}</script></head>',
    )
    read = read_profile(_target(Source.INSTAGRAM), html)
    assert read.bio_link == "https://blushbarbyhos.com"


# --------------------------------------------------------------------------- #
# §6.6's operating rules, enforced rather than assumed
# --------------------------------------------------------------------------- #


def _renderer(pages: dict[str, tuple[str, int]], calls: list[str]):
    async def render(url: str) -> tuple[str, int]:
        calls.append(url)
        return pages.get(url, ("", 404))

    return render


def _instant_policy() -> PacingPolicy:
    """§6.6's 8-20s band is the real setting; a test must not sleep for it."""
    return PacingPolicy(delay_min=0.0, delay_max=0.0, concurrency=1)


def test_one_request_per_business_per_run_is_enforced_by_the_source():
    """§6.6's hard cap, and it is enforced here rather than trusted to the caller.

    A business holding both a Facebook and an Instagram URL is the normal case —
    248 businesses have a social URL and 133 have both — so the easy mistake is
    to render two pages for one business. Facebook wins because §6.7 measured it
    at 58% WhatsApp buttons against Instagram's 10%.
    """
    business = uuid.uuid4()
    calls: list[str] = []
    pages = {
        "https://facebook.com/x": (_FB_WITH_BUTTON, 200),
        "https://instagram.com/x": (_IG_HEAD, 200),
    }
    source = SocialSource(
        cache=None,
        policy=_instant_policy(),
        renderer=_renderer(pages, calls),
    )
    targets = [
        _target(Source.FACEBOOK, "https://facebook.com/x", business),
        _target(Source.INSTAGRAM, "https://instagram.com/x", business),
    ]

    harvest = asyncio.run(source.harvest(targets))

    assert calls == ["https://facebook.com/x"]
    assert harvest.requests == 1
    assert len(harvest.reads) == 1


def test_two_branches_sharing_one_page_both_get_the_evidence():
    """§5.2's "one crawl per domain, not per business", applied to Pages.

    Chains share a Facebook Page — 21 Facebook URLs across Lahore × salon's 21
    businesses are 17 distinct Pages. Skipping the duplicate URL renders once and
    gives the second branch *nothing*, which looks like politeness and is §5.5's
    failure mode: a business quietly enriched with zero rows. One render, two
    reads, one request.
    """
    calls: list[str] = []
    source = SocialSource(
        cache=None,
        policy=_instant_policy(),
        renderer=_renderer({"https://facebook.com/chain": (_FB_WITH_BUTTON, 200)}, calls),
    )
    branch_a, branch_b = uuid.uuid4(), uuid.uuid4()
    targets = [
        _target(Source.FACEBOOK, "https://facebook.com/chain", branch_a),
        _target(Source.FACEBOOK, "https://facebook.com/chain", branch_b),
    ]

    harvest = asyncio.run(source.harvest(targets))

    assert calls == ["https://facebook.com/chain"], "one request, not two"
    assert len(harvest.reads) == 2
    assert {r.target.business_id for r in harvest.reads} == {branch_a, branch_b}
    assert all(r.button_numbers == ("+923211220011",) for r in harvest.reads)


def test_pages_without_og_tags_do_not_trip_the_breaker():
    """The bug the first Lahore x food run found, and it cost 29 Pages.

    ``CircuitBreaker`` implements §5.5's "the selectors stopped matching" rule as
    5 consecutive unproductive successes. Counting "this Page has no og tags" as
    unproductive tripped the Facebook breaker 77 profiles in — while **all 77
    renders had returned HTTP 200**. Facebook refused nothing; 11 of the 77
    simply render without og tags, and 5 landed in a row.

    14% of Pages behaving that way is a property of Facebook's population, not a
    signal about our parser, so the source no longer treats it as one. The real
    §5.5 check moved to the stage, where "rendered 15+ and found nothing" is
    genuinely diagnostic.
    """
    bare = '<html><head><title>Facebook</title></head><body></body></html>'
    calls: list[str] = []
    urls = [f"https://facebook.com/{i}" for i in range(8)]
    source = SocialSource(
        cache=None,
        policy=_instant_policy(),
        renderer=_renderer({u: (bare, 200) for u in urls}, calls),
    )
    targets = [_target(Source.FACEBOOK, u, uuid.uuid4()) for u in urls]

    harvest = asyncio.run(source.harvest(targets))

    assert calls == urls, "a 200 without og tags is still a successful request"
    assert harvest.blocked is False
    assert all(not r.rendered for r in harvest.reads)


def test_a_shell_response_is_not_cached():
    """A 200 carrying no profile must not occupy the cache for 30 days.

    Measured on Lahore × food: 45 of 140 profiles came back as the application
    shell, and re-requesting one later rendered it fine — Instagram soft-gates
    under load. §6.6's 30-day TTL would turn one transient gate into a month of
    permanent misses, with the re-run "hitting cache", finding nothing, and
    reporting the business as having no bio.

    This is the one place §7's "save every raw response" is deliberately not
    applied, for §7's own reason: bodies are kept so a broken selector can be
    re-parsed, and a shell has nothing to re-parse.
    """

    class _RecordingCache:
        def __init__(self) -> None:
            self.puts: list[str] = []

        def get(self, url, **kwargs):
            return None

        def put(self, url, body, **kwargs):
            self.puts.append(url)
            return url

    shell = "<html><head><title>Instagram</title></head><body></body></html>"
    cache = _RecordingCache()
    source = SocialSource(
        cache=cache,
        policy=_instant_policy(),
        renderer=_renderer(
            {
                "https://instagram.com/shell": (shell, 200),
                "https://instagram.com/real": (_IG_HEAD, 200),
            },
            [],
        ),
    )

    asyncio.run(
        source.harvest(
            [
                _target(Source.INSTAGRAM, "https://instagram.com/shell", uuid.uuid4()),
                _target(Source.INSTAGRAM, "https://instagram.com/real", uuid.uuid4()),
            ]
        )
    )

    assert cache.puts == ["https://instagram.com/real?__render=1"]


def test_an_instagram_profilecard_url_is_normalised_to_the_profile():
    """``/profilecard/`` is the share-card view and renders with no bio at all.

    It looks identical to a soft-gated shell in the run stats, which is how it
    hid. Measured: the same handle without the suffix renders fully. Tracking
    params (`igshid`, `igsh`, `utm_*`) were tested alongside it and make no
    difference, so they are left alone rather than stripped on suspicion.
    """
    assert (
        canonical_profile_url(
            "https://www.instagram.com/_fresh.grounds_/profilecard/", Source.INSTAGRAM
        )
        == "https://www.instagram.com/_fresh.grounds_"
    )
    # Untouched on Facebook, and untouched when the suffix is not there.
    plain = "https://www.instagram.com/_fresh.grounds_"
    assert canonical_profile_url(plain, Source.INSTAGRAM) == plain
    fb = "https://facebook.com/x/profilecard/"
    assert canonical_profile_url(fb, Source.FACEBOOK) == fb


def test_a_429_stops_the_module_for_the_run():
    """§6.6: honour it, then stop — do not back off into the same wall.

    The rule is explicit that a refusal ends the module for that run, so the
    third target must never be requested.
    """
    calls: list[str] = []
    pages = {
        "https://facebook.com/a": (_FB_WITH_BUTTON, 200),
        "https://facebook.com/b": ("", 429),
        "https://facebook.com/c": (_FB_WITH_BUTTON, 200),
    }
    source = SocialSource(
        cache=None, policy=_instant_policy(), renderer=_renderer(pages, calls)
    )
    targets = [
        _target(Source.FACEBOOK, f"https://facebook.com/{k}", uuid.uuid4())
        for k in ("a", "b", "c")
    ]

    harvest = asyncio.run(source.harvest(targets))

    assert calls == ["https://facebook.com/a", "https://facebook.com/b"]
    assert harvest.refused is True
    assert harvest.error == "http_429"


def test_a_blocked_record_is_recorded_not_dropped():
    """§6.6: "a blocked record is a valid outcome, not a failure to route around."

    §5.5's failure mode is a source that quietly returns nothing, so a profile
    the breaker refused to fetch must still appear in the harvest.
    """
    breaker = CircuitBreaker(source=Source.FACEBOOK)
    breaker.record_blocked(503)
    source = SocialSource(
        cache=None,
        policy=_instant_policy(),
        breakers={Source.FACEBOOK: breaker, Source.INSTAGRAM: CircuitBreaker(
            source=Source.INSTAGRAM
        )},
        renderer=_renderer({}, []),
    )

    harvest = asyncio.run(
        source.harvest([_target(Source.FACEBOOK, "https://facebook.com/a")])
    )

    assert harvest.blocked is True
    assert len(harvest.reads) == 1
    assert harvest.reads[0].blocked is True


def test_an_open_facebook_breaker_does_not_silence_instagram():
    """§7: "continue the run with remaining sources" — and they are two sources.

    One breaker per platform, because each is a single host: §7's per-source rule
    applies literally here, unlike §5.2 where one module fans out over hundreds
    of unrelated hosts. This is the distinction §6.6's "stop the module" must not
    swallow — a *refusal* ends the module for the run, an already-open breaker on
    one platform only skips that platform.
    """
    fb = CircuitBreaker(source=Source.FACEBOOK)
    fb.record_blocked(503)
    calls: list[str] = []
    source = SocialSource(
        cache=None,
        policy=_instant_policy(),
        breakers={
            Source.FACEBOOK: fb,
            Source.INSTAGRAM: CircuitBreaker(source=Source.INSTAGRAM),
        },
        renderer=_renderer({"https://instagram.com/b": (_IG_HEAD, 200)}, calls),
    )

    harvest = asyncio.run(
        source.harvest(
            [
                _target(Source.FACEBOOK, "https://facebook.com/a", uuid.uuid4()),
                _target(Source.INSTAGRAM, "https://instagram.com/b", uuid.uuid4()),
            ]
        )
    )

    assert calls == ["https://instagram.com/b"]
    # Still reported: the Facebook profile is work the run intended and skipped,
    # which §13 Screen 2 must not collapse into `done`.
    assert harvest.blocked is True
    assert [r.blocked for r in harvest.reads] == [True, False]


def test_renders_are_cached_under_their_own_key():
    """A fetched body and a rendered body are different artifacts.

    §6.7 measured the fetched Instagram body as a 605 KB JS shell and the
    fetched Facebook body as an HTTP 400 error page, and both are in the §7
    archive under the plain URL from the recon spike. Sharing the key would make
    the module "hit cache" on a body that provably contains nothing.
    """
    assert render_cache_url("https://facebook.com/x") == "https://facebook.com/x?__render=1"
    assert (
        render_cache_url("https://instagram.com/x?hl=en")
        == "https://instagram.com/x?hl=en&__render=1"
    )


# --------------------------------------------------------------------------- #
# Real markup
# --------------------------------------------------------------------------- #


def test_a_real_rendered_instagram_profile_parses():
    """Against the actual bytes Instagram served, trimmed only for size.

    The inline shapes above are readable; this is the one that catches Meta
    reshuffling the markup, which §6.7 expects them to do.
    """
    body = gzip.decompress((FIXTURES / "social_instagram_profile.html.gz").read_bytes())
    read = read_profile(
        _target(Source.INSTAGRAM, "https://www.instagram.com/blushbarbyhos"), body
    )

    assert read.rendered is True
    assert read.page_name is not None and "Blush Bar" in read.page_name
    assert read.bio_text is not None
    # The measured facts for this profile: a number in the bio *and* a wa.me
    # bio link carrying the same number.
    assert "+923190155026" in [p.e164 for p in read.bio_phones]
    assert "+923190155026" in read.button_numbers


def test_a_real_rendered_facebook_page_yields_its_whatsapp_button():
    """The 58% case, against the bytes Facebook actually served.

    Worth pinning on real markup rather than a literal: the button is buried in
    a JSON blob with escaped slashes and a signed token, which is exactly the
    shape a hand-written fixture would simplify away.
    """
    body = gzip.decompress((FIXTURES / "social_facebook_page.html.gz").read_bytes())
    read = read_profile(
        _target(Source.FACEBOOK, "https://facebook.com/ChaiStudio2016"), body
    )

    assert read.rendered is True
    assert read.button_numbers == ("+923211220011",)
    findings = findings_for(read)
    assert findings[0].evidence.label is WhatsAppLabel.CONFIRMED


# --------------------------------------------------------------------------- #
# The merge, against a real database
# --------------------------------------------------------------------------- #


def _run(session: Session) -> Run:
    run = Run(
        id=uuid.uuid4(),
        mode="discovery",
        city="Lahore",
        category="food",
        number_pref=NumberPreference.OWNER_FIRST,
        sources_enabled={"google_maps": True, "facebook": True},
        status=RunStatus.RUNNING,
    )
    session.add(run)
    session.flush()
    return run


def _business(session: Session, run: Run, **kwargs) -> Business:
    business = Business(
        id=uuid.uuid4(),
        run_id=run.id,
        name=kwargs.pop("name", "Chai Studio"),
        name_norm=kwargs.pop("name_norm", "chai studio"),
        city="Lahore",
        **kwargs,
    )
    session.add(business)
    session.flush()
    return business


class _StubSource:
    """Stands in for ``SocialSource`` so the merge is tested without a browser."""

    def __init__(self, reads: list[ProfileRead]) -> None:
        self._reads = reads

    async def harvest(self, targets):
        from leadscraper.sources.social import SocialHarvest

        return SocialHarvest(reads=self._reads, requests=len(self._reads))


@pytest.mark.usefixtures("db_session")
def test_a_facebook_button_upgrades_a_maps_number_to_confirmed(db_session: Session):
    """The whole point of Phase 8, on the row it is meant to move.

    Maps gives a bare ``03xx`` which §9.3 scores 0.60 *likely*. The Page's
    WhatsApp button is 0.90 *confirmed*, worth +12 points under §10.2's 30-weight
    term — which clears the ≥60 bar outright for the 80 businesses sitting in the
    50-59 band.
    """
    run = _run(db_session)
    business = _business(db_session, run, facebook_url="https://facebook.com/ChaiStudio2016")
    business.contacts.append(
        Contact(
            business_id=business.id,
            kind=ContactKind.PHONE,
            value_raw="0321 1220011",
            value_e164="+923211220011",
            line_type=LineType.MOBILE,
            wa_evidence=0.60,
            wa_label=WhatsAppLabel.LIKELY,
            belongs_to=BelongsTo.BUSINESS,
            confidence=0.70,
            source=Source.GOOGLE_MAPS,
            source_url="https://maps.google.com/?cid=1",
            scraped_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    read = read_profile(
        _target(
            Source.FACEBOOK, "https://facebook.com/ChaiStudio2016", business.id
        ),
        _FB_WITH_BUTTON,
    )
    report = enrich_socials(db_session, run, source=_StubSource([read]))

    contact = business.contacts[0]
    assert len(business.contacts) == 1, "an upgrade is not a second row"
    assert contact.wa_label == WhatsAppLabel.CONFIRMED
    assert float(contact.wa_evidence) == pytest.approx(0.90)
    assert contact.wa_evidence_url == "https://facebook.com/ChaiStudio2016"
    # §1: provenance survives. The number still came from Maps; only the proof
    # came from Facebook, and those are two different columns for a reason.
    assert contact.source == Source.GOOGLE_MAPS
    assert contact.source_url == "https://maps.google.com/?cid=1"
    assert report.contacts_upgraded == 1
    assert report.confirmed_whatsapp == 1


@pytest.mark.usefixtures("db_session")
def test_a_contact_stuck_at_unknown_line_type_is_repaired(db_session: Session):
    """``unknown`` is a gap, not a value, and §10.2 punishes it.

    An earlier pass wrote three live rows as `confirmed` + `unknown`, and §10.2
    qualifies on "≥60 **and a mobile**" — so the most valuable rows this stage
    produces were the ones the export would drop. Guarding the gap-fill on
    ``is None`` alone never repaired them, because the column held the string.
    """
    run = _run(db_session)
    business = _business(db_session, run, facebook_url="https://facebook.com/x")
    business.contacts.append(
        Contact(
            business_id=business.id,
            kind=ContactKind.PHONE,
            value_raw="+923211220011",
            value_e164="+923211220011",
            line_type=LineType.UNKNOWN,
            wa_evidence=0.90,
            wa_label=WhatsAppLabel.CONFIRMED,
            belongs_to=BelongsTo.BUSINESS,
            confidence=0.90,
            source=Source.FACEBOOK,
            source_url="https://facebook.com/x",
            scraped_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    read = read_profile(
        _target(Source.FACEBOOK, "https://facebook.com/x", business.id), _FB_WITH_BUTTON
    )
    enrich_socials(db_session, run, source=_StubSource([read]))

    assert business.contacts[0].line_type == LineType.MOBILE
    # The evidence it already had is untouched — this is a gap-fill, not a rescore.
    assert business.contacts[0].wa_label == WhatsAppLabel.CONFIRMED


@pytest.mark.usefixtures("db_session")
def test_a_profile_without_a_button_never_lowers_existing_evidence(db_session: Session):
    """Evidence only ever moves up. A Page that happens not to show a WhatsApp
    button is not evidence *against* a number §5.2 already confirmed."""
    run = _run(db_session)
    business = _business(db_session, run, instagram_url="https://instagram.com/x")
    business.contacts.append(
        Contact(
            business_id=business.id,
            kind=ContactKind.PHONE,
            value_raw="+923190155026",
            value_e164="+923190155026",
            line_type=LineType.MOBILE,
            wa_evidence=1.00,
            wa_label=WhatsAppLabel.CONFIRMED,
            wa_evidence_url="https://blushbarbyhos.com/contact",
            belongs_to=BelongsTo.BUSINESS,
            confidence=0.95,
            source=Source.BUSINESS_WEBSITE,
            source_url="https://blushbarbyhos.com",
            scraped_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    read = read_profile(
        _target(Source.INSTAGRAM, "https://instagram.com/x", business.id), _IG_HEAD
    )
    enrich_socials(db_session, run, source=_StubSource([read]))

    contact = business.contacts[0]
    assert float(contact.wa_evidence) == pytest.approx(1.00)
    assert contact.wa_label == WhatsAppLabel.CONFIRMED
    assert contact.wa_evidence_url == "https://blushbarbyhos.com/contact"


@pytest.mark.usefixtures("db_session")
def test_a_number_the_profile_publishes_and_maps_did_not_is_a_new_contact(
    db_session: Session,
):
    """Never discard a contact (§10.1). This is the route for the 47 businesses
    across the seven runs that carry no phone number at all."""
    run = _run(db_session)
    business = _business(db_session, run, instagram_url="https://instagram.com/x")
    db_session.flush()

    read = read_profile(
        _target(Source.INSTAGRAM, "https://instagram.com/x", business.id), _IG_HEAD
    )
    report = enrich_socials(db_session, run, source=_StubSource([read]))

    assert report.contacts_added == 1
    contact = business.contacts[0]
    assert contact.value_e164 == "+923190155026"
    assert contact.source == Source.INSTAGRAM
    assert contact.wa_label == WhatsAppLabel.LIKELY
    # §8 is Phase 9. A bio naming the owner is tempting and this stage leaves it.
    assert contact.person_name is None
    assert contact.belongs_to == BelongsTo.BUSINESS


@pytest.mark.usefixtures("db_session")
def test_a_store_bio_link_gap_fills_the_website_for_section_5_2(db_session: Session):
    """§6.4's real branch distribution, measured: stores 15, socials 8, wa.me 2,
    link-in-bio hubs **0** across 32 profiles.

    So a store URL is handed to §5.2 — which is built, budgeted and tested —
    rather than this module growing a second crawler. For the 97 businesses with
    a social profile and no website, this is the step that gives them one.
    """
    run = _run(db_session)
    business = _business(db_session, run, instagram_url="https://instagram.com/x")
    db_session.flush()

    html = _IG_HEAD.replace(
        "</head>",
        '<script>{"external_url":"https://blushbarbyhos.com"}</script></head>',
    )
    read = read_profile(
        _target(Source.INSTAGRAM, "https://instagram.com/x", business.id), html
    )
    report = enrich_socials(db_session, run, source=_StubSource([read]))

    assert business.website == "https://blushbarbyhos.com/"
    assert report.websites_filled == 1


@pytest.mark.usefixtures("db_session")
def test_a_facebook_page_linking_its_instagram_fills_it_free(db_session: Session):
    """The feeder §6 never costed: 6 of 12 Facebook bio links are Instagram
    profiles. No SERP credit, no name matching, no join test — the Page is
    telling us which account is its own.

    Worth contrasting with §6.3's paid alternative, which measured at 30% recall
    and needed a bare-handle filter to reach usable precision at all.
    """
    run = _run(db_session)
    business = _business(db_session, run, facebook_url="https://facebook.com/x")
    db_session.flush()

    html = _FB_WITH_BUTTON.replace(
        "</body>",
        '<a href="https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.instagram.com'
        '%2Fchaistudio&h=AT2">IG</a></body>',
    )
    read = read_profile(
        _target(Source.FACEBOOK, "https://facebook.com/x", business.id), html
    )
    report = enrich_socials(db_session, run, source=_StubSource([read]))

    assert business.instagram_url == "https://www.instagram.com/chaistudio"
    assert report.socials_filled == 1


@pytest.mark.usefixtures("db_session")
def test_an_existing_website_is_never_overwritten_by_a_bio_link(db_session: Session):
    """Gap-fill, not overwrite. A business's own domain outranks whatever its
    Instagram bio happens to point at this month."""
    run = _run(db_session)
    business = _business(
        db_session,
        run,
        instagram_url="https://instagram.com/x",
        website="https://the-real-site.pk/",
    )
    db_session.flush()

    html = _IG_HEAD.replace(
        "</head>",
        '<script>{"external_url":"https://linktr.ee/someone-else"}</script></head>',
    )
    read = read_profile(
        _target(Source.INSTAGRAM, "https://instagram.com/x", business.id), html
    )
    report = enrich_socials(db_session, run, source=_StubSource([read]))

    assert business.website == "https://the-real-site.pk/"
    assert report.websites_filled == 0

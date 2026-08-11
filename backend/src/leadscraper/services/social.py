"""Stage 3 orchestration — §6 Facebook/Instagram, Tier 3 (Phase 8).

`confirmed` WhatsApp is the scarcest thing this system produces. Before this
stage, 45 of 898 businesses across the seven runs had one, and every route to it
ran through §5.2 — the business's own website. 97 businesses hold a social URL
and **no website at all**, so for them this stage is the only route to a
confirmed number that will ever exist.

What §6.7 measured, and how it shapes this file:

* **Facebook first.** 58% of rendered FB Pages carry an
  ``api.whatsapp.com/send?phone=`` button (§9.3's 0.90 row) against 10% of IG
  profiles. §6 and §16 order Instagram first; the measurement says otherwise and
  ``_targets_for`` follows the measurement.
* **Instagram's contribution is different, not larger.** Half of IG bios print a
  mobile in plain text, but a bare mobile is §9.3 0.60 — the score 850 of 898
  businesses already carry. It is worth real money for the 47 businesses with no
  phone at all and is close to a constant elsewhere, so it is harvested and not
  oversold.
* **No bio-hub follower is built.** §6.4 says a bio link is "virtually always"
  a Linktree/beacons.ai hub with a WhatsApp button. Measured across 32 profiles:
  **zero** bio links were a link-in-bio hub. The real distribution is stores
  (15), other socials (8), wa.me (2), none (7). So a store bio-link gap-fills
  ``business.website`` and §5.2 — which exists, is tested, and does this well —
  picks it up, rather than this module growing a second crawler for a population
  that is not there.

The merge rules are §5.2's, and they are the same rules for the same reason:

* **Never discard a contact.** A number the profile publishes that Maps did not
  is a new row.
* **Only ever upgrade evidence.** A profile that happens not to show a WhatsApp
  button is not evidence *against* a number.
* **Never rewrite provenance.** ``source``/``source_url`` say the number came
  from the profile; ``wa_evidence_url`` says which page proved it.
* **Gap-fill, never overwrite.** Person names are Stage 4's territory (§8) and
  this module does not build an attribution engine inside itself.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from leadscraper.config import get_settings
from leadscraper.core.cache import FetchCache
from leadscraper.core.phone import ParsedPhone, normalise
from leadscraper.core.textnorm import classify_site_url
from leadscraper.core.whatsapp import (
    WaEvidence,
    WaSignal,
    baseline_signal,
    label_for,
    mentions_whatsapp_near,
    score_signals,
)
from leadscraper.db.models import Business, Contact, Run
from leadscraper.db.session import session_scope
from leadscraper.enums import (
    BelongsTo,
    ContactKind,
    LineType,
    RunStatus,
    Source,
    Stage,
    WhatsAppLabel,
)
from leadscraper.logging import get_logger
from leadscraper.pipeline.stages import StageResult
from leadscraper.sources.social import (
    ProfileRead,
    SocialHarvest,
    SocialSource,
    SocialTarget,
)
from leadscraper.sources.website import normalise_website

log = get_logger(__name__)

# A profile is one contact block, not a directory. A bio listing more than this
# many distinct numbers is a franchise index (Rina's Kitchenette prints one per
# branch, which is legitimate and is where this ceiling came from) and beyond it
# we are harvesting a list rather than a lead.
MAX_PHONES_PER_PROFILE = 8

# §10.2 "how sure are we this is really the business's number" — a different
# question from §9.3's "does it take WhatsApp". A platform's own WhatsApp button
# is a structured field the page owner filled in; a number typed into free-text
# bio prose is weaker, and sits where §5.2 puts free text.
BUTTON_CONFIDENCE = 0.90
BIO_TEXT_CONFIDENCE = 0.60

# §5.5 at the stage level. If this many profiles rendered and the whole stage
# produced no number, the extractors have stopped matching — Meta reshuffles this
# markup often, and the failure mode to avoid is a clean-looking zero.
YIELD_FLOOR_PROFILES = 15


@dataclass(slots=True)
class SocialReport:
    businesses_total: int = 0
    with_social: int = 0
    profiles_requested: int = 0
    profiles_rendered: int = 0
    profiles_empty: int = 0
    profiles_failed: int = 0
    profiles_blocked: int = 0
    facebook_read: int = 0
    instagram_read: int = 0
    requests: int = 0
    from_cache: int = 0
    phones_found: int = 0
    contacts_added: int = 0
    contacts_upgraded: int = 0
    confirmed_whatsapp: int = 0
    wa_buttons_found: int = 0
    bio_numbers_found: int = 0
    websites_filled: int = 0
    socials_filled: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "businesses_total": self.businesses_total,
            "with_social": self.with_social,
            "profiles_requested": self.profiles_requested,
            "profiles_rendered": self.profiles_rendered,
            "profiles_empty": self.profiles_empty,
            "profiles_failed": self.profiles_failed,
            "profiles_blocked": self.profiles_blocked,
            "facebook_read": self.facebook_read,
            "instagram_read": self.instagram_read,
            "requests": self.requests,
            "from_cache": self.from_cache,
            "phones_found": self.phones_found,
            "contacts_added": self.contacts_added,
            "contacts_upgraded": self.contacts_upgraded,
            "confirmed_whatsapp": self.confirmed_whatsapp,
            "wa_buttons_found": self.wa_buttons_found,
            "bio_numbers_found": self.bio_numbers_found,
            "websites_filled": self.websites_filled,
            "socials_filled": self.socials_filled,
        }


# --------------------------------------------------------------------------- #
# One profile's numbers, scored
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SocialFinding:
    """One number off one profile, with everything the ``contacts`` row needs."""

    e164: str
    raw: str
    line_type: LineType
    operator: str | None
    confidence: float
    evidence: WaEvidence
    evidence_url: str
    source_url: str
    source: Source

    @property
    def is_confirmed(self) -> bool:
        return self.evidence.label is WhatsAppLabel.CONFIRMED


def findings_for(read: ProfileRead) -> list[SocialFinding]:
    """Score every number one profile published (§9.3).

    A WhatsApp button beats a bio number for the same phone, so the two passes
    run button-first and the bio pass skips what the button already settled —
    the same "strongest evidence wins, evidence does not accumulate" rule
    ``score_signals`` applies within a single number.
    """
    findings: list[SocialFinding] = []
    seen: set[str] = set()
    buttons = list(read.button_numbers)
    bio = read.bio_text or ""

    for e164 in buttons:
        if e164 in seen:
            continue
        seen.add(e164)
        # A button number usually appears nowhere in the bio text, so there is
        # no ParsedPhone to borrow — normalise the E.164 to classify it. Leaving
        # it UNKNOWN is not harmless: §10.2 qualifies a lead on "≥60 **and a
        # mobile**" and §3.3's `whatsapp_only` ranks on line type, so the single
        # most valuable row this stage produces — a confirmed WhatsApp number —
        # would be the one row filtered out of the export.
        parsed = _parsed_in(read.bio_phones, e164) or normalise(e164)
        # §9.3's platform row, at 0.90 rather than the 1.00 link row. See the
        # scoring note in ``sources/social.py``: on a Meta property these are the
        # same artifact and the platform-specific row is the more specific one.
        evidence = score_signals([WaSignal.PLATFORM_BUTTON])
        findings.append(
            SocialFinding(
                e164=e164,
                raw=parsed.raw if parsed else e164,
                line_type=parsed.line_type if parsed else LineType.UNKNOWN,
                operator=parsed.operator if parsed else None,
                confidence=BUTTON_CONFIDENCE,
                evidence=evidence,
                evidence_url=read.target.url,
                source_url=read.target.url,
                source=read.target.platform,
            )
        )

    for parsed in read.bio_phones[:MAX_PHONES_PER_PROFILE]:
        if parsed.e164 in seen:
            continue
        seen.add(parsed.e164)
        signals = {baseline_signal(parsed.line_type)}
        # §9.3's 0.75 row. The bio is short enough that a 50-char window is a
        # genuine adjacency test rather than "somewhere on the page".
        if parsed.span and mentions_whatsapp_near(bio, parsed.span):
            signals.add(WaSignal.TEXT_PROXIMITY)
        score = score_signals(signals)
        findings.append(
            SocialFinding(
                e164=parsed.e164,
                raw=parsed.raw,
                line_type=parsed.line_type,
                operator=parsed.operator,
                confidence=BIO_TEXT_CONFIDENCE,
                evidence=WaEvidence(score.score, label_for(score.score), score.signal),
                evidence_url=read.target.url,
                source_url=read.target.url,
                source=read.target.platform,
            )
        )

    findings.sort(key=lambda f: (-f.evidence.score, -f.confidence, f.e164))
    return findings[:MAX_PHONES_PER_PROFILE]


def _parsed_in(phones: tuple[ParsedPhone, ...], e164: str) -> ParsedPhone | None:
    return next((p for p in phones if p.e164 == e164), None)


# --------------------------------------------------------------------------- #
# The stage
# --------------------------------------------------------------------------- #


def run_social_enrichment(run_id: uuid.UUID, limit: int | None = None) -> StageResult:
    """Render every discovered business's FB Page and IG profile, once each."""
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"No such run: {run_id}")

        report = enrich_socials(session, run, limit)
        run.stats = {**(run.stats or {}), "social_enrichment": report.as_dict()}
        session.flush()

    return StageResult(
        stage=Stage.SOCIAL_ENRICHMENT,
        run_id=run_id,
        processed=report.profiles_rendered,
        produced=report.contacts_added + report.contacts_upgraded,
        skipped=report.businesses_total - report.with_social,
        failed=report.profiles_failed + report.profiles_blocked,
        notes={k: str(v) for k, v in report.as_dict().items()},
    )


def enrich_socials(
    session: Session,
    run: Run,
    limit: int | None = None,
    source: SocialSource | None = None,
) -> SocialReport:
    """The stage body, against a caller-supplied session.

    Split out exactly as ``enrich_run`` is, so the merge rules can be tested
    against a real database with an injected renderer and no browser.
    """
    report = SocialReport()
    settings = get_settings()

    businesses = list(
        session.execute(
            select(Business)
            .where(Business.run_id == run.id)
            .options(selectinload(Business.contacts))
            .order_by(Business.created_at)
        ).scalars()
    )
    report.businesses_total = len(businesses)
    by_id = {business.id: business for business in businesses}

    targets = _targets_for(businesses, report)
    if limit is not None:
        # Limit by *business*, not by target, so a limit never renders a
        # business's Facebook Page and drops its Instagram profile — that would
        # make a truncated run look like a platform that yielded nothing.
        keep = list(dict.fromkeys(t.business_id for t in targets))[:limit]
        allowed = set(keep)
        targets = [t for t in targets if t.business_id in allowed]
    report.profiles_requested = len(targets)

    if not targets:
        log.warning("social.no_targets", run_id=str(run.id), businesses=len(businesses))
        return report

    source = source or SocialSource(
        cache=FetchCache(session=session, settings=settings),
        settings=settings,
        # Persist each rendered body as it arrives rather than at the end of a
        # 45-minute transaction. See the note on ``SocialSource.__init__``.
        on_cached=session.commit,
    )
    harvest: SocialHarvest = asyncio.run(source.harvest(targets))
    report.requests = harvest.requests
    report.from_cache = harvest.from_cache

    for read in harvest.reads:
        _tally(report, read)
        business = by_id.get(read.target.business_id)
        if business is None or not read.rendered:
            continue
        _apply_profile(business, read, report)

    session.flush()
    _finalise(run, report, harvest)
    return report


def _targets_for(businesses: list[Business], report: SocialReport) -> list[SocialTarget]:
    """Facebook first, then Instagram — §6.7, not §6's stated ordering.

    Both platforms for one business are emitted; ``SocialSource.harvest``
    enforces §6.6's one-request-per-business cap, so with the default cap of 1
    the Facebook Page wins and the Instagram profile is skipped. That is the
    intended behaviour and it is why the ordering lives here: raise
    ``social_requests_per_business`` to 2 and the same list reads both.
    """
    facebook: list[SocialTarget] = []
    instagram: list[SocialTarget] = []
    for business in businesses:
        has_any = False
        if business.facebook_url:
            has_any = True
            facebook.append(
                SocialTarget(
                    business_id=business.id,
                    name=business.name,
                    platform=Source.FACEBOOK,
                    url=business.facebook_url,
                )
            )
        if business.instagram_url:
            has_any = True
            instagram.append(
                SocialTarget(
                    business_id=business.id,
                    name=business.name,
                    platform=Source.INSTAGRAM,
                    url=business.instagram_url,
                )
            )
        if has_any:
            report.with_social += 1
    return facebook + instagram


def _tally(report: SocialReport, read: ProfileRead) -> None:
    # Cache hits are counted on the harvest, not here — a cached read is still a
    # profile that was read, and double-counting it as a request would make the
    # §6.6 request cap unverifiable from the run stats.
    if read.target.platform is Source.FACEBOOK:
        report.facebook_read += 1
    else:
        report.instagram_read += 1

    if read.blocked:
        report.profiles_blocked += 1
    elif not read.rendered:
        report.profiles_failed += 1
    elif read.has_findings:
        report.profiles_rendered += 1
    else:
        # Rendered, parsed, no number. Extremely common and legitimate — half of
        # profiles simply do not print one — but counted apart so a stage that is
        # *all* empties is visible rather than merely quiet.
        report.profiles_rendered += 1
        report.profiles_empty += 1


# --------------------------------------------------------------------------- #
# Applying one profile to one business
# --------------------------------------------------------------------------- #


def _apply_profile(business: Business, read: ProfileRead, report: SocialReport) -> None:
    findings = findings_for(read)
    report.phones_found += len(findings)
    report.wa_buttons_found += len(read.button_numbers)
    report.bio_numbers_found += sum(
        1 for p in read.bio_phones if p.e164 not in set(read.button_numbers)
    )

    existing = {
        contact.value_e164: contact
        for contact in business.contacts
        if contact.kind == ContactKind.PHONE and contact.value_e164
    }

    for finding in findings:
        contact = existing.get(finding.e164)
        if contact is None:
            new = _new_contact(business, finding)
            # Appended to the relationship, not added to the session — the same
            # reason as §5.2's: `session.add` alone leaves `business.contacts`
            # stale and the next profile for the same business would insert the
            # number twice.
            business.contacts.append(new)
            existing[finding.e164] = new
            report.contacts_added += 1
            if finding.is_confirmed:
                report.confirmed_whatsapp += 1
        elif _upgrade_contact(contact, finding, report):
            report.contacts_upgraded += 1

    _apply_bio_link(business, read, report)


def _new_contact(business: Business, finding: SocialFinding) -> Contact:
    return Contact(
        business_id=business.id,
        kind=ContactKind.PHONE,
        value_raw=finding.raw,
        value_e164=finding.e164,
        line_type=finding.line_type,
        operator=finding.operator,
        wa_evidence=round(finding.evidence.score, 2),
        wa_label=finding.evidence.label,
        wa_evidence_url=finding.evidence_url,
        # §8 is Phase 9. A bio naming the owner is tempting and this module does
        # not touch it — fabricating the name↔number join is the one thing §8
        # forbids outright.
        belongs_to=BelongsTo.BUSINESS,
        confidence=round(finding.confidence, 2),
        source=finding.source,
        source_url=finding.source_url,
        scraped_at=datetime.now(UTC),
    )


def _upgrade_contact(contact: Contact, finding: SocialFinding, report: SocialReport) -> bool:
    """Fold profile evidence into a contact another source already produced.

    Upgrade-only on every field, for §5.2's reason: a Facebook Page carrying a
    WhatsApp button for a number Maps listed is new information; the same Page
    *not* carrying one is not.
    """
    changed = False

    if finding.evidence.score > float(contact.wa_evidence or 0.0):
        was_confirmed = contact.wa_label == WhatsAppLabel.CONFIRMED
        contact.wa_evidence = round(finding.evidence.score, 2)
        contact.wa_label = finding.evidence.label
        contact.wa_evidence_url = finding.evidence_url
        changed = True
        if finding.is_confirmed and not was_confirmed:
            report.confirmed_whatsapp += 1

    if finding.confidence > float(contact.confidence or 0.0):
        contact.confidence = round(finding.confidence, 2)
        changed = True

    # ``unknown`` means the same thing as ``NULL`` here — "no source has told us
    # what kind of line this is" — so learning the real type is a gap-fill, not
    # an overwrite. Guarding on ``is None`` alone left three live rows stuck at
    # `confirmed` + `unknown`, which §10.2 then declines to qualify because it
    # requires "≥60 **and a mobile**".
    if (
        contact.line_type in (None, LineType.UNKNOWN)
        and finding.line_type is not None
        and finding.line_type is not LineType.UNKNOWN
    ):
        contact.line_type = finding.line_type
        contact.operator = contact.operator or finding.operator
        changed = True

    return changed


def _apply_bio_link(business: Business, read: ProfileRead, report: SocialReport) -> None:
    """Route the bio link to whoever owns it. Gap-fill only.

    §6.4's three-way branch, corrected by §6.7's measurement of where these links
    actually go: a store URL is the common case and belongs to §5.2, another
    social profile is the free cross-platform feeder, and a link-in-bio hub —
    §6.4's headline case — did not occur once in 32 profiles.
    """
    if not read.bio_link:
        return

    website, facebook, instagram = classify_site_url(read.bio_link)

    if facebook and not business.facebook_url:
        business.facebook_url = facebook
        report.socials_filled += 1
    if instagram and not business.instagram_url:
        # The Facebook → Instagram feeder. It costs nothing and needs no join
        # test: the Page is telling us which account is its own.
        business.instagram_url = instagram
        report.socials_filled += 1

    if website and not business.website:
        normalised = normalise_website(website)
        if normalised:
            # Handed to §5.2 rather than crawled here. A re-run of Stage 2 picks
            # it up with the crawler that is already built, budgeted and tested,
            # and for the 97 businesses with a social profile and no website this
            # is the step that gives them a website at all.
            business.website = normalised
            report.websites_filled += 1


def _finalise(run: Run, report: SocialReport, harvest: SocialHarvest) -> None:
    """Report the stage honestly, and never upgrade a status an earlier stage set."""
    degraded: str | None = None

    attempted = report.profiles_rendered + report.profiles_failed

    if report.profiles_rendered >= YIELD_FLOOR_PROFILES and report.phones_found == 0:
        degraded = (
            f"social enrichment rendered {report.profiles_rendered} profiles and "
            f"extracted no phone numbers — the §6 extractors have almost certainly "
            f"stopped matching (implementation.md §5.5, §6.7)"
        )
        log.error(
            "social.zero_yield", run_id=str(run.id), rendered=report.profiles_rendered
        )
    elif attempted >= YIELD_FLOOR_PROFILES and report.profiles_rendered == 0:
        # The other half of the §5.5 check, and the half the source deliberately
        # stopped making. A single Page without og tags is ordinary — 11 of 77
        # Facebook Pages are like that (§6.7). *Every* page lacking them means
        # Meta reshuffled the markup or started serving us a shell, and that is
        # a fact about the run, not about the Pages.
        degraded = (
            f"social enrichment fetched {attempted} profiles and rendered none of "
            f"them — the og:title/description shapes §6.7 measured are gone "
            f"(implementation.md §5.5)"
        )
        log.error("social.nothing_rendered", run_id=str(run.id), attempted=attempted)
    elif harvest.refused:
        # §6.6: we stopped the module for the run on purpose. That is the correct
        # behaviour and it is still work the run planned and did not do, which is
        # the distinction §13 Screen 2 must not collapse into `done`.
        degraded = (
            f"the social module stopped for this run after {harvest.error} — "
            f"§6.6 requires honouring a refusal rather than backing off into it"
        )
    elif report.profiles_blocked:
        degraded = (
            f"{report.profiles_blocked} profiles were never rendered — the "
            f"platform circuit breaker opened or the daily budget ran out"
        )

    if degraded:
        run.status = RunStatus.PARTIAL
        run.error = run.error or degraded

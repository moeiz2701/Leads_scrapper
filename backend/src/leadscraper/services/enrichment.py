"""Stage 2 orchestration — enriching discovered businesses from their own sites.

This is the stage that changes what the run is worth. After Stage 1 every number
in the table is a §9.3 *likely* at best, because a bare ``03xx`` from a Maps
listing is all the evidence there is. §5.2 is the only source that can move a
number to `confirmed`, and it does it by finding the business's own ``wa.me``
link — so this stage is where the WhatsApp column stops being an inference.

The merge rules are §10.1's, applied to contacts rather than businesses:

* **Never discard a contact.** A number the site publishes that Maps did not is
  a new row, not a correction.
* **Only ever upgrade evidence.** Website evidence raises a Maps contact's
  §9.3 score and records which page proved it; it never lowers one. A site that
  happens not to mention WhatsApp is not evidence *against* a number.
* **Never rewrite provenance.** ``source``/``source_url`` keep saying where the
  number came from; ``wa_evidence_url`` says where the proof came from.
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
from leadscraper.core.site_evidence import PhoneFinding, SiteEvidence
from leadscraper.db.models import Business, Contact, Run
from leadscraper.db.session import session_scope
from leadscraper.enums import (
    BelongsTo,
    ContactKind,
    PersonRole,
    RunStatus,
    Source,
    Stage,
    WhatsAppLabel,
)
from leadscraper.logging import get_logger
from leadscraper.pipeline.stages import StageResult
from leadscraper.sources.website import SiteCrawl, WebsiteSource, normalise_website

log = get_logger(__name__)

# §12.1 has one `email` column. Sites that list five addresses are listing
# departments; keeping a few gives the exporter a choice without turning the
# contacts table into a mailing list.
MAX_EMAILS_PER_BUSINESS = 3

EMAIL_CONFIDENCE = 0.80

# §5.5, at the run level rather than the request level. If this many domains
# answered with real pages and the whole stage produced no phone number, the
# extractors have stopped matching — that is the "1,500 blank rows and nobody
# notices" scenario, and the run says so instead of reporting success.
YIELD_FLOOR_SITES = 20


@dataclass(slots=True)
class EnrichmentReport:
    businesses_total: int = 0
    with_website: int = 0
    domains_crawled: int = 0
    sites_ok: int = 0
    sites_empty: int = 0
    sites_failed: int = 0
    # The host said no. A legitimate per-record outcome — some SMB sites sit
    # behind a WAF — and not a reason to call the stage degraded.
    sites_refused: int = 0
    # We never crawled it: the source breaker was open or the budget was spent.
    # This *is* work the run intended to do and did not.
    sites_blocked: int = 0
    pages_fetched: int = 0
    pages_from_cache: int = 0
    phones_found: int = 0
    contacts_added: int = 0
    contacts_upgraded: int = 0
    confirmed_whatsapp: int = 0
    emails_added: int = 0
    socials_filled: int = 0
    people_linked: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "businesses_total": self.businesses_total,
            "with_website": self.with_website,
            "domains_crawled": self.domains_crawled,
            "sites_ok": self.sites_ok,
            "sites_empty": self.sites_empty,
            "sites_failed": self.sites_failed,
            "sites_refused": self.sites_refused,
            "sites_blocked": self.sites_blocked,
            "pages_fetched": self.pages_fetched,
            "pages_from_cache": self.pages_from_cache,
            "phones_found": self.phones_found,
            "contacts_added": self.contacts_added,
            "contacts_upgraded": self.contacts_upgraded,
            "confirmed_whatsapp": self.confirmed_whatsapp,
            "emails_added": self.emails_added,
            "socials_filled": self.socials_filled,
            "people_linked": self.people_linked,
        }


def run_website_enrichment(run_id: uuid.UUID, limit: int | None = None) -> StageResult:
    """Crawl every discovered business's website and fold the evidence in."""
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"No such run: {run_id}")

        report = enrich_run(session, run, limit)
        run.stats = {**(run.stats or {}), "website_enrichment": report.as_dict()}
        session.flush()

    return StageResult(
        stage=Stage.CONTACT_ENRICHMENT,
        run_id=run_id,
        processed=report.domains_crawled,
        produced=report.contacts_added + report.contacts_upgraded,
        skipped=report.businesses_total - report.with_website,
        failed=report.sites_failed + report.sites_blocked,
        notes={k: str(v) for k, v in report.as_dict().items()},
    )


def enrich_run(
    session: Session,
    run: Run,
    limit: int | None = None,
    source: WebsiteSource | None = None,
) -> EnrichmentReport:
    """The stage body, against a caller-supplied session.

    Split out from ``run_website_enrichment`` so the merge rules can be tested
    against a real database without a network, by handing in a crawler.
    """
    report = EnrichmentReport()
    settings = get_settings()

    businesses = list(
        session.execute(
            # Contacts are eager-loaded because every business is about to have
            # its existing numbers compared against the site's — lazy-loading
            # that is one query per business across the whole run.
            select(Business)
            .where(Business.run_id == run.id)
            .options(selectinload(Business.contacts))
            .order_by(Business.created_at)
        ).scalars()
    )
    report.businesses_total = len(businesses)

    # One crawl per domain, not per business. Chains and franchises share a site,
    # and a second crawl of the same domain would be pure duplicated politeness
    # cost even though the cache would answer it.
    by_domain: dict[str, list[Business]] = {}
    for business in businesses:
        base = normalise_website(business.website)
        if base is None:
            continue
        report.with_website += 1
        by_domain.setdefault(base, []).append(business)

    targets = list(by_domain)
    if limit is not None:
        targets = targets[:limit]
    report.domains_crawled = len(targets)

    if not targets:
        log.warning("website.no_domains", run_id=str(run.id), businesses=len(businesses))
        return report

    source = source or WebsiteSource(
        cache=FetchCache(session=session, settings=settings), settings=settings
    )
    crawls: list[SiteCrawl] = asyncio.run(source.crawl_many(targets))

    # gather() preserves argument order, so a crawl lines up with the domain key
    # it was launched for — no need to re-derive the key from the crawl.
    for domain, crawl in zip(targets, crawls, strict=True):
        evidence = crawl.evidence()
        _tally(report, crawl, evidence)
        report.phones_found += len(evidence.phones)
        for business in by_domain[domain]:
            _apply_site_evidence(session, business, evidence, report)

    session.flush()
    _finalise(run, report)
    return report


def _tally(report: EnrichmentReport, crawl: SiteCrawl, evidence: SiteEvidence) -> None:
    report.pages_fetched += crawl.fetched
    report.pages_from_cache += crawl.from_cache
    if crawl.blocked:
        report.sites_blocked += 1
    elif crawl.refused:
        report.sites_refused += 1
    elif not crawl.pages:
        report.sites_failed += 1
    elif evidence.has_findings:
        report.sites_ok += 1
    else:
        # Answered, parsed, nothing to show. Common and legitimate — plenty of
        # small sites are a splash image — but counted separately so a stage
        # that is *all* empties is visible in the run stats.
        report.sites_empty += 1


# --------------------------------------------------------------------------- #
# Applying evidence to one business
# --------------------------------------------------------------------------- #


def _apply_site_evidence(
    session: Session,
    business: Business,
    evidence: SiteEvidence,
    report: EnrichmentReport,
) -> None:
    existing = {
        contact.value_e164: contact
        for contact in business.contacts
        if contact.kind == ContactKind.PHONE and contact.value_e164
    }

    for finding in evidence.phones:
        contact = existing.get(finding.e164)
        if contact is None:
            new = _new_contact(business, finding)
            # Appended to the relationship rather than added to the session, so
            # the already-loaded collection stays truthful. `session.add` alone
            # leaves `business.contacts` stale, and the next pass over the same
            # business would then insert the number a second time.
            business.contacts.append(new)
            existing[finding.e164] = new
            report.contacts_added += 1
            if finding.is_confirmed:
                report.confirmed_whatsapp += 1
            if finding.person_name:
                report.people_linked += 1
        elif _upgrade_contact(contact, finding, report):
            report.contacts_upgraded += 1

    _apply_emails(session, business, evidence, report)
    _apply_socials(business, evidence, report)


def _new_contact(business: Business, finding: PhoneFinding) -> Contact:
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
        person_name=finding.person_name,
        person_role=finding.person_role,
        attribution=finding.attribution,
        # §8: a named founder's number belongs to the owner. Everything else is
        # the business's published line — Tier B's "the listed number *is* the
        # owner's" inference is Stage 4's call, not this stage's.
        belongs_to=(
            BelongsTo.OWNER if finding.person_role is PersonRole.OWNER else BelongsTo.BUSINESS
        ),
        confidence=round(finding.confidence, 2),
        source=Source.BUSINESS_WEBSITE,
        source_url=finding.source_url,
        scraped_at=datetime.now(UTC),
    )


def _upgrade_contact(contact: Contact, finding: PhoneFinding, report: EnrichmentReport) -> bool:
    """Fold website evidence into a contact another source already produced.

    Upgrade-only, on every field. The website confirming a Maps number is new
    information; the website *not* mentioning WhatsApp is not, so a lower score
    here never overwrites a higher one recorded earlier.
    """
    changed = False

    current = float(contact.wa_evidence or 0.0)
    if finding.evidence.score > current:
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

    # Gap-fill only: Stage 4 owns attribution and must not find this stage's
    # guess sitting where its own answer belongs.
    if finding.person_name and not contact.person_name:
        contact.person_name = finding.person_name
        contact.person_role = finding.person_role
        contact.attribution = finding.attribution
        if contact.belongs_to in (None, BelongsTo.BUSINESS) and (
            finding.person_role is PersonRole.OWNER
        ):
            contact.belongs_to = BelongsTo.OWNER
        report.people_linked += 1
        changed = True

    if contact.line_type is None and finding.line_type is not None:
        contact.line_type = finding.line_type
        contact.operator = finding.operator
        changed = True

    return changed


def _apply_emails(
    session: Session,
    business: Business,
    evidence: SiteEvidence,
    report: EnrichmentReport,
) -> None:
    existing = {
        (contact.value_raw or "").lower()
        for contact in business.contacts
        if contact.kind == ContactKind.EMAIL
    }
    room = MAX_EMAILS_PER_BUSINESS - len(existing)
    if room <= 0:
        return

    source_url = evidence.website
    for email in evidence.emails[:room]:
        if email in existing:
            continue
        existing.add(email)
        business.contacts.append(
            Contact(
                business_id=business.id,
                kind=ContactKind.EMAIL,
                value_raw=email,
                confidence=EMAIL_CONFIDENCE,
                belongs_to=BelongsTo.BUSINESS,
                source=Source.BUSINESS_WEBSITE,
                source_url=source_url,
                scraped_at=datetime.now(UTC),
            )
        )
        report.emails_added += 1


def _apply_socials(
    business: Business, evidence: SiteEvidence, report: EnrichmentReport
) -> None:
    """Fill the §6 hand-off columns. Gap-fill, matching the §10.1 merge rule."""
    if evidence.facebook_url and not business.facebook_url:
        business.facebook_url = evidence.facebook_url
        report.socials_filled += 1
    if evidence.instagram_url and not business.instagram_url:
        business.instagram_url = evidence.instagram_url
        report.socials_filled += 1


def _finalise(run: Run, report: EnrichmentReport) -> None:
    """Report the stage honestly, and never upgrade a status Stage 1 downgraded."""
    degraded: str | None = None

    if report.sites_ok >= YIELD_FLOOR_SITES and report.phones_found == 0:
        degraded = (
            f"website enrichment parsed {report.sites_ok} live sites and extracted "
            f"no phone numbers — the §5.2 extractors have almost certainly stopped "
            f"matching (implementation.md §5.5)"
        )
        log.error("website.zero_yield", run_id=str(run.id), sites_ok=report.sites_ok)
    elif report.sites_blocked:
        # Not "a host refused us" — that is `sites_refused` and it is a normal
        # per-record outcome. This is work the run planned and skipped, which is
        # the thing §13 Screen 2 must not report as `done`.
        degraded = (
            f"{report.sites_blocked} domains were never crawled — the source "
            f"circuit breaker opened or the daily budget ran out"
        )

    if degraded:
        run.status = RunStatus.PARTIAL
        run.error = run.error or degraded

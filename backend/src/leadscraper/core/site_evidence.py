"""Folding a domain's crawled pages into scored contact evidence (§5.2, §9.3).

One page is not the unit of truth — a *site* is. The homepage prints the number
in the header, the contact page carries the ``wa.me`` link, and the JSON-LD block
names the founder. §9.3 scores a number by the strongest evidence anywhere on
the site, so this module unions the pages before it scores anything.

Pure, like ``webparse``: page extracts in, findings out, no network and no
database. §9.3's rule that we never probe a number for WhatsApp holds by
construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from leadscraper.core.phone import ParsedPhone, normalise
from leadscraper.core.webparse import PageExtract
from leadscraper.core.whatsapp import WaEvidence, evaluate
from leadscraper.enums import Attribution, LineType, PersonRole, WhatsAppLabel


class PhoneOrigin(StrEnum):
    """Which §5.2 shape first produced the number. Kept for provenance."""

    WA_LINK = "wa_link"
    WIDGET = "widget"
    JSONLD = "jsonld"
    TEL = "tel"
    TEXT = "text"


# ``contact_confidence`` for §10.2, i.e. "how sure are we this is really the
# business's number" — a different question from §9.3's "does it take WhatsApp".
#
# This is not quite §5.2's printed 1–6 order. That list ranks *WhatsApp*
# evidence, which is why it puts JSON-LD last; for contact confidence a
# machine-readable ``telephone`` inside a structured business record is stronger
# than a free-text digit run, so it sits above the regex path here. The two
# rankings answer different questions and both are applied.
ORIGIN_CONFIDENCE: dict[PhoneOrigin, float] = {
    PhoneOrigin.WA_LINK: 0.95,
    PhoneOrigin.WIDGET: 0.95,
    PhoneOrigin.JSONLD: 0.90,
    PhoneOrigin.TEL: 0.85,
    PhoneOrigin.TEXT: 0.60,
}

_ORIGIN_RANK = list(ORIGIN_CONFIDENCE)

# §12.1 exports four ranked phone slots. A site offering more than a dozen
# distinct valid numbers is a listings page, not a business's contact block, and
# keeping all of them would bury the real one under branch numbers.
MAX_PHONES_PER_SITE = 12

# schema.org keys that imply ownership. "employee" and "member" name a person
# but not an owner, so §8's rule against fabricating the join means they are
# captured and left unattributed rather than promoted.
_OWNER_PERSON_KEYS = frozenset({"founder", "founders"})

# §8 Tier D covers "site About/Team page, JSON-LD founder" at 0.40–0.70 name
# confidence. A structured record is the top of that band, not the middle.
JSONLD_PERSON_CONFIDENCE = 0.70


@dataclass(frozen=True, slots=True)
class PhoneFinding:
    """One number, with every field the ``contacts`` row needs."""

    e164: str
    raw: str
    line_type: LineType
    operator: str | None
    origin: PhoneOrigin
    confidence: float
    evidence: WaEvidence
    # The page that produced the *strongest* WhatsApp signal. Not necessarily
    # where the number was first seen: a contact page's wa.me link routinely
    # confirms a number printed in the homepage header, and §1 says every record
    # carries the URL its evidence came from.
    evidence_url: str
    source_url: str
    person_name: str | None = None
    person_role: PersonRole | None = None
    attribution: Attribution | None = None

    @property
    def is_confirmed(self) -> bool:
        return self.evidence.label is WhatsAppLabel.CONFIRMED


@dataclass(slots=True)
class SiteEvidence:
    """What a whole domain yielded."""

    website: str
    phones: list[PhoneFinding] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    facebook_url: str | None = None
    instagram_url: str | None = None
    pages_parsed: int = 0

    @property
    def has_findings(self) -> bool:
        return bool(self.phones or self.emails)

    @property
    def confirmed_phones(self) -> list[PhoneFinding]:
        return [p for p in self.phones if p.is_confirmed]


@dataclass(slots=True)
class _Candidate:
    parsed: ParsedPhone
    origin: PhoneOrigin
    source_url: str


def build_site_evidence(website: str, pages: list[PageExtract]) -> SiteEvidence:
    """Score every number the crawl found against every signal the crawl found."""
    evidence = SiteEvidence(website=website, pages_parsed=len(pages))
    if not pages:
        return evidence

    candidates = _collect_candidates(pages)
    people = _owner_people(pages)

    findings: list[PhoneFinding] = []
    for candidate in candidates.values():
        wa, wa_url = _best_wa_evidence(candidate.parsed, pages)
        name, role = people.get(candidate.parsed.e164, (None, None))
        findings.append(
            PhoneFinding(
                e164=candidate.parsed.e164,
                raw=candidate.parsed.raw,
                line_type=candidate.parsed.line_type,
                operator=candidate.parsed.operator,
                origin=candidate.origin,
                confidence=ORIGIN_CONFIDENCE[candidate.origin],
                evidence=wa,
                evidence_url=wa_url,
                source_url=candidate.source_url,
                person_name=name,
                person_role=role,
                # §8: name and number came out of the same structured record, so
                # the join is `linked` — observed, not inferred, and not
                # `confirmed`, which §8 reserves for a paired listing.
                attribution=Attribution.LINKED if name else None,
            )
        )

    findings.sort(key=lambda f: (-f.evidence.score, -f.confidence, f.e164))
    evidence.phones = findings[:MAX_PHONES_PER_SITE]
    evidence.emails = _union_emails(pages)
    evidence.facebook_url = next((p.facebook_url for p in pages if p.facebook_url), None)
    evidence.instagram_url = next((p.instagram_url for p in pages if p.instagram_url), None)
    return evidence


def _collect_candidates(pages: list[PageExtract]) -> dict[str, _Candidate]:
    """Every distinct number on the site, tagged with its strongest origin.

    A number reached by several shapes keeps the best one: ``tel:`` on the
    homepage and ``wa.me`` on the contact page is one number with a wa_link
    origin, not two contacts.
    """
    candidates: dict[str, _Candidate] = {}

    def offer(parsed: ParsedPhone | None, origin: PhoneOrigin, url: str) -> None:
        if parsed is None:
            return
        existing = candidates.get(parsed.e164)
        if existing is None:
            candidates[parsed.e164] = _Candidate(parsed, origin, url)
        elif _ORIGIN_RANK.index(origin) < _ORIGIN_RANK.index(existing.origin):
            existing.origin = origin

    for page in pages:
        for e164 in page.wa_numbers:
            offer(normalise(e164), PhoneOrigin.WA_LINK, page.url)
        for e164 in page.widget_numbers:
            offer(normalise(e164), PhoneOrigin.WIDGET, page.url)
        for parsed in page.jsonld_numbers:
            offer(parsed, PhoneOrigin.JSONLD, page.url)
        for parsed in page.tel_numbers:
            offer(parsed, PhoneOrigin.TEL, page.url)
        for parsed in page.text_phones:
            offer(parsed, PhoneOrigin.TEXT, page.url)

    return candidates


def _best_wa_evidence(parsed: ParsedPhone, pages: list[PageExtract]) -> tuple[WaEvidence, str]:
    """§9.3 across the whole crawl — the strongest signal on any page wins."""
    best: WaEvidence | None = None
    best_url = pages[0].url

    for page in pages:
        span = next(
            (p.span for p in page.text_phones if p.e164 == parsed.e164),
            None,
        )
        current = evaluate(
            page.text,
            parsed.e164,
            span,
            parsed.line_type,
            wa_numbers=page.wa_numbers,
            widget_numbers=page.widget_numbers,
        )
        if best is None or current.score > best.score:
            best, best_url = current, page.url

    assert best is not None  # pages is non-empty by the caller's guard
    return best, best_url


def _owner_people(pages: list[PageExtract]) -> dict[str, tuple[str, PersonRole]]:
    """Map a number to a person, but only inside one JSON-LD record.

    §8: "a name and a number are only *linked* when they appear in the same DOM
    block or the same structured record." A founder named in one block and a
    phone printed in the footer are not a pair, and this function will not make
    them one.
    """
    linked: dict[str, tuple[str, PersonRole]] = {}
    for page in pages:
        for block in page.jsonld:
            owners = [name for name, key in block.people if key in _OWNER_PERSON_KEYS]
            if not owners or not block.telephones:
                continue
            for telephone in block.telephones:
                parsed = normalise(telephone)
                if parsed and parsed.e164 not in linked:
                    linked[parsed.e164] = (owners[0], PersonRole.OWNER)
    return linked


def _union_emails(pages: list[PageExtract]) -> list[str]:
    seen: set[str] = set()
    emails: list[str] = []
    for page in pages:
        for email in page.emails:
            if email not in seen:
                seen.add(email)
                emails.append(email)
    return emails

"""Parsing one page of a business's own website (§5.2).

§5.2 calls this "the WhatsApp confirmation engine", and that is not a flourish:
it is the **only** source in the design that can produce a `confirmed` label.
Maps gives us a number and §9.3 scores a bare ``03xx`` at 0.60 — *likely*. A
business publishing ``wa.me/923001234567`` on its own site is telling you the
number takes WhatsApp, and that is the 1.00 row of the §9.3 table.

This module is pure: HTML in, extracted fields out, **no network calls of any
kind**. That is deliberate on two counts — it makes every shape below testable
from a string literal, and §9.3's rule that we never probe a number for WhatsApp
is easier to keep when the scoring path physically cannot reach the network.

Extraction follows §5.2's priority order, and each shape is here because it
appears on real Pakistani SMB sites, not because it is in a spec.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from selectolax.parser import HTMLParser

from leadscraper.core.phone import ParsedPhone, extract_phones, normalise
from leadscraper.core.textnorm import registrable_domain
from leadscraper.core.whatsapp import extract_wa_numbers

# --------------------------------------------------------------------------- #
# §5.2 item 2 — floating WhatsApp chat widgets
# --------------------------------------------------------------------------- #

# The attribute the number actually sits in varies by widget vendor. These are
# the ones observed in the wild; unknown vendors fall through to the tel:/text
# paths rather than being guessed at.
_WIDGET_ATTRS = (
    "data-phone",
    "data-number",
    "data-phone-number",
    "data-whatsapp",
    "data-wa-number",
    "data-wanumber",
    "data-telephone",
)

_WIDGET_ATTR_SELECTOR = ", ".join(f"[{attr}]" for attr in _WIDGET_ATTRS)

# A ``data-phone`` on its own proves nothing — plenty of themes put one on a
# click-to-call button. §9.3 scores the *widget* at 0.95 only when the markup
# says WhatsApp, so we require a WhatsApp marker on the element or one of its
# near ancestors. ``ht-ctc`` is the WordPress "Click to Chat" plugin, which is by
# far the most common WhatsApp widget on PK SMB sites.
_WIDGET_HINT_RE = re.compile(
    r"whats\s?app|wa[-_](?:chat|widget|btn|button|link|number|float)"
    r"|ht[-_]ctc|click[-_]?to[-_]?chat|floating[-_]?wa",
    re.IGNORECASE,
)

_WIDGET_ANCESTOR_DEPTH = 3

# --------------------------------------------------------------------------- #
# §5.2 item 5 — email and social profiles
# --------------------------------------------------------------------------- #

_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
)

# Addresses that belong to the toolchain, not the business. Harvesting these
# means emailing Sentry about a Lahore salon.
_EMAIL_NOISE_DOMAINS = frozenset(
    {
        "example.com", "example.org", "domain.com", "yourdomain.com",
        "email.com", "sentry.io", "sentry.wixpress.com", "wix.com",
        "wordpress.org", "wordpress.com", "godaddy.com", "squarespace.com",
        "schema.org", "w3.org", "googleapis.com", "gstatic.com",
    }
)

# "logo@2x.png" and friends match the email shape and are not emails.
_EMAIL_NOISE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")

_INSTAGRAM_HOSTS = ("instagram.com", "instagr.am")
_FACEBOOK_HOSTS = ("facebook.com", "fb.com", "fb.me")

# Share/intent URLs point at *our* page, not at the business's profile.
_SOCIAL_NOISE_RE = re.compile(
    r"/(?:sharer|share|share\.php|dialog|plugins|tr/?$|intent)", re.IGNORECASE
)

# --------------------------------------------------------------------------- #
# §5.2 crawl budget — homepage, /contact*, /about*
# --------------------------------------------------------------------------- #

_CONTACT_PATH_RE = re.compile(
    r"/(?:contact|contact-?us|get-?in-?touch|reach-?us|connect)(?:[-/._]|$)", re.IGNORECASE
)
_ABOUT_PATH_RE = re.compile(
    r"/(?:about|about-?us|who-?we-?are|our-?story|our-?team|team)(?:[-/._]|$)", re.IGNORECASE
)
_CONTACT_TEXT_RE = re.compile(r"contact|get in touch|reach us|رابطہ", re.IGNORECASE)
_ABOUT_TEXT_RE = re.compile(r"\babout\b|our story|our team|who we are", re.IGNORECASE)

_NON_PAGE_SUFFIXES = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".mp4",
    ".doc", ".docx", ".xls", ".xlsx", ".css", ".js", ".ico", ".xml",
)

# --------------------------------------------------------------------------- #
# §5.2 item 6 — JSON-LD
# --------------------------------------------------------------------------- #

_JSONLD_BUSINESS_TYPES = frozenset(
    {
        "organization", "corporation", "store", "restaurant", "cafe", "bakery",
        "beautysalon", "hairsalon", "dayspa", "healthandbeautybusiness",
        "autorepair", "automotivebusiness", "realestateagent", "professionalservice",
        "foodestablishment", "sportsactivitylocation", "place", "medicalclinic",
    }
)

# schema.org keys that name a human. §5.2: "occasionally yields owner name".
_JSONLD_PERSON_KEYS = ("founder", "founders", "employee", "employees", "member", "author")

_JSONLD_MAX_DEPTH = 8


@dataclass(frozen=True, slots=True)
class JsonLdBusiness:
    """One ``LocalBusiness``/``Organization`` node, flattened to what we use."""

    name: str | None
    telephones: tuple[str, ...]
    emails: tuple[str, ...]
    # (person name, schema.org key) — the key is kept raw so the caller decides
    # what role it implies. §8 forbids fabricating a join; "employee" is not
    # "owner" and this module does not pretend otherwise.
    people: tuple[tuple[str, str], ...]
    same_as: tuple[str, ...]


@dataclass(slots=True)
class PageExtract:
    """Everything one page yields. Numbers are E.164; ``text_phones`` keep spans.

    ``text`` is the page's visible text with script/style removed, and the spans
    in ``text_phones`` index into *that* string — which is what makes §9.3's
    "WhatsApp within 50 chars of the number" proximity check meaningful.
    """

    url: str
    text: str = ""
    wa_numbers: list[str] = field(default_factory=list)
    widget_numbers: list[str] = field(default_factory=list)
    tel_numbers: list[ParsedPhone] = field(default_factory=list)
    text_phones: list[ParsedPhone] = field(default_factory=list)
    jsonld_numbers: list[ParsedPhone] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    facebook_url: str | None = None
    instagram_url: str | None = None
    jsonld: list[JsonLdBusiness] = field(default_factory=list)
    crawl_targets: list[str] = field(default_factory=list)

    @property
    def has_any_contact(self) -> bool:
        """Did this page yield anything at all? Feeds the §5.5 empty check."""
        return bool(
            self.wa_numbers
            or self.widget_numbers
            or self.tel_numbers
            or self.text_phones
            or self.jsonld_numbers
            or self.emails
        )


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #

_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.IGNORECASE
)
_CONTENT_TYPE_CHARSET_RE = re.compile(r"charset\s*=\s*([A-Za-z0-9_\-]+)", re.IGNORECASE)


def decode_body(body: bytes, content_type: str | None = None) -> str:
    """Bytes to text, honouring the declared charset.

    Worth doing properly rather than assuming UTF-8: a business name in Urdu
    mangled at this step is mangled in the CSV, and §12.2 already goes to the
    trouble of a BOM so Excel renders it. Order is Content-Type header, then the
    ``<meta charset>`` the page declares, then UTF-8 with replacement — which
    never raises, so a badly-labelled page still yields its phone numbers.
    """
    candidates: list[str] = []
    if content_type:
        match = _CONTENT_TYPE_CHARSET_RE.search(content_type)
        if match:
            candidates.append(match.group(1))
    meta = _META_CHARSET_RE.search(body[:4096])
    if meta:
        candidates.append(meta.group(1).decode("ascii", errors="ignore"))
    candidates.append("utf-8")

    for encoding in candidates:
        try:
            return body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Individual extractors
# --------------------------------------------------------------------------- #


def visible_text(tree: HTMLParser) -> str:
    """Page text with script/style/noscript stripped.

    Scripts must go before the §9.1 text regex runs: analytics blobs are full of
    long digit runs, and a tracking ID that happens to parse as a landline is a
    fabricated lead.
    """
    for node in tree.css("script, style, noscript, template"):
        node.decompose()
    body = tree.body or tree.root
    return body.text(separator="\n") if body else ""


def extract_widget_numbers(tree: HTMLParser) -> list[str]:
    """§5.2 item 2 / §9.3 0.95 — chat widgets carrying a ``data-phone``."""
    found: list[str] = []
    seen: set[str] = set()

    for node in tree.css(_WIDGET_ATTR_SELECTOR):
        if not _looks_like_whatsapp_widget(node):
            continue
        for attr in _WIDGET_ATTRS:
            raw = node.attributes.get(attr)
            if not raw:
                continue
            parsed = normalise(raw)
            if parsed and parsed.e164 not in seen:
                seen.add(parsed.e164)
                found.append(parsed.e164)
    return found


def _looks_like_whatsapp_widget(node) -> bool:
    """Does this element, or a near ancestor, say WhatsApp?

    Required because ``data-phone`` alone is also how themes mark up plain
    click-to-call buttons, and scoring one of those at 0.95 would put a landline
    in the `confirmed` column.
    """
    current = node
    for _ in range(_WIDGET_ANCESTOR_DEPTH + 1):
        if current is None:
            return False
        blob = " ".join(
            [current.tag or ""]
            + [f"{k} {v or ''}" for k, v in (current.attributes or {}).items()]
        )
        if _WIDGET_HINT_RE.search(blob):
            return True
        current = current.parent
    return False


def extract_tel_numbers(tree: HTMLParser) -> list[ParsedPhone]:
    """§5.2 item 3 — ``a[href^="tel:"]``. Explicit intent, so above free text."""
    found: list[ParsedPhone] = []
    seen: set[str] = set()
    for node in tree.css('a[href^="tel:"], a[href^="TEL:"], a[href^="callto:"]'):
        href = node.attributes.get("href") or ""
        raw = href.split(":", 1)[1].split("?")[0].strip()
        parsed = normalise(raw)
        if parsed and parsed.e164 not in seen:
            seen.add(parsed.e164)
            found.append(parsed)
    return found


def extract_emails(tree: HTMLParser, text: str) -> list[str]:
    """§5.2 item 5 — ``mailto:``, Cloudflare-obfuscated, then free text."""
    found: list[str] = []
    seen: set[str] = set()

    def add(candidate: str | None) -> None:
        if not candidate:
            return
        email = candidate.strip().strip(".,;:<>()[]").lower()
        if not _is_plausible_email(email) or email in seen:
            return
        seen.add(email)
        found.append(email)

    for node in tree.css('a[href^="mailto:"], a[href^="MAILTO:"]'):
        href = node.attributes.get("href") or ""
        add(href.split(":", 1)[1].split("?")[0])

    for node in tree.css("[data-cfemail]"):
        add(decode_cfemail(node.attributes.get("data-cfemail") or ""))

    for match in _EMAIL_RE.finditer(text):
        add(match.group(0))

    return found


def decode_cfemail(encoded: str) -> str | None:
    """Undo Cloudflare's email obfuscation.

    Not a circumvention of an access control (§5.5's line): the page renders this
    address to every visitor, Cloudflare just defers the assembly to JavaScript.
    We do the same trivial XOR the page's own script does. §5.3 records that
    UrduPoint obfuscates this way too, so Phase 6 inherits it.
    """
    try:
        data = bytes.fromhex(encoded.strip())
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    return "".join(chr(byte ^ key) for byte in data[1:])


def _is_plausible_email(email: str) -> bool:
    if email.count("@") != 1 or " " in email:
        return False
    if email.endswith(_EMAIL_NOISE_SUFFIXES):
        return False
    domain = email.rsplit("@", 1)[1]
    if domain in _EMAIL_NOISE_DOMAINS:
        return False
    return "." in domain and len(domain) > 3


def extract_socials(tree: HTMLParser, base_url: str) -> tuple[str | None, str | None]:
    """First real Facebook and Instagram *profile* link on the page.

    Feeds Stage 3 (§6). Share buttons are excluded — a ``facebook.com/sharer``
    URL points back at the page we are already reading.
    """
    facebook: str | None = None
    instagram: str | None = None

    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        host = registrable_domain(absolute) or ""
        if _SOCIAL_NOISE_RE.search(urlsplit(absolute).path):
            continue
        if facebook is None and host.endswith(_FACEBOOK_HOSTS):
            facebook = absolute
        elif instagram is None and host.endswith(_INSTAGRAM_HOSTS):
            instagram = absolute
        if facebook and instagram:
            break

    return facebook, instagram


def extract_jsonld(tree: HTMLParser) -> list[JsonLdBusiness]:
    """§5.2 item 6 — ``LocalBusiness``/``Organization`` blocks.

    The highest-quality shape on the page when present: name, phone and person
    arrive in one structured record, which is exactly the §8 condition for
    calling a name↔number join `linked` rather than guessed.
    """
    businesses: list[JsonLdBusiness] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(deep=True) or ""
        if not raw.strip():
            continue
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # Hand-written JSON-LD is frequently malformed. One bad block must
            # not cost us the rest of the page.
            continue
        _walk_jsonld(document, businesses, 0)
    return businesses


def _walk_jsonld(node: object, out: list[JsonLdBusiness], depth: int) -> None:
    if depth > _JSONLD_MAX_DEPTH:
        return
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, out, depth + 1)
        return
    if not isinstance(node, dict):
        return

    if _is_jsonld_business(node):
        out.append(_build_jsonld_business(node))

    # ``@graph`` is just another key, so the generic descent covers it.
    for value in node.values():
        if isinstance(value, list | dict):
            _walk_jsonld(value, out, depth + 1)


def _jsonld_types(node: dict) -> set[str]:
    raw = node.get("@type") or node.get("type")
    values = raw if isinstance(raw, list) else [raw]
    return {v.lower() for v in values if isinstance(v, str)}


def _is_jsonld_business(node: dict) -> bool:
    types = _jsonld_types(node)
    if any("business" in t for t in types) or types & _JSONLD_BUSINESS_TYPES:
        return True
    # Untyped or oddly-typed blocks still count when they carry a name and a
    # phone — that is the payload we want, whatever the author labelled it.
    return bool(node.get("telephone")) and bool(node.get("name"))


def _as_str_tuple(value: object) -> tuple[str, ...]:
    items = value if isinstance(value, list) else [value]
    return tuple(str(v).strip() for v in items if isinstance(v, str | int) and str(v).strip())


def _build_jsonld_business(node: dict) -> JsonLdBusiness:
    people: list[tuple[str, str]] = []
    for key in _JSONLD_PERSON_KEYS:
        value = node.get(key)
        if value is None:
            continue
        for item in value if isinstance(value, list) else [value]:
            name = item.get("name") if isinstance(item, dict) else item
            if isinstance(name, str) and name.strip():
                people.append((name.strip(), key))

    name = node.get("name")
    return JsonLdBusiness(
        name=name.strip() if isinstance(name, str) else None,
        telephones=_as_str_tuple(node.get("telephone")),
        emails=_as_str_tuple(node.get("email")),
        people=tuple(people),
        same_as=_as_str_tuple(node.get("sameAs")),
    )


def find_crawl_targets(tree: HTMLParser, base_url: str, limit: int = 3) -> list[str]:
    """§5.2 crawl budget — the ``/contact*`` and ``/about*`` pages worth fetching.

    Only links the page actually offers, never guessed paths: probing ``/contact``
    on every domain buys a 404 per site for the ones that call it something else,
    and the contact link is in the nav or footer of essentially every site that
    has one. Contact pages rank above about pages because that is where the
    number is; §5.2's priority order is about *shapes*, this is about *pages*.
    """
    origin_domain = registrable_domain(base_url)
    contact: list[str] = []
    about: list[str] = []
    seen: set[str] = set()

    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "callto:")):
            continue

        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        if registrable_domain(absolute) != origin_domain:
            continue

        path = parts.path.lower()
        if path.endswith(_NON_PAGE_SUFFIXES):
            continue

        clean = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if parts.query:
            clean = f"{clean}?{parts.query}"
        if clean in seen or clean.rstrip("/") == base_url.rstrip("/"):
            continue
        seen.add(clean)

        label = (node.text(deep=True) or "").strip()[:80]
        if _CONTACT_PATH_RE.search(path) or _CONTACT_TEXT_RE.search(label):
            contact.append(clean)
        elif _ABOUT_PATH_RE.search(path) or _ABOUT_TEXT_RE.search(label):
            about.append(clean)

    return (contact + about)[:limit]


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def parse_page(url: str, body: bytes | str, content_type: str | None = None) -> PageExtract:
    """Run every §5.2 extractor over one page.

    Order matters in one place only: ``visible_text`` decomposes script and style
    nodes, so it runs before the free-text phone and email passes. Everything
    else reads the tree independently.
    """
    html = decode_body(body, content_type) if isinstance(body, bytes) else body
    tree = HTMLParser(html)

    extract = PageExtract(url=url)
    # wa.me links are read from the *raw* markup, not the tree: they turn up in
    # inline scripts and onclick handlers as often as in href attributes, and
    # visible_text() has already thrown those away by the time it returns.
    extract.wa_numbers = extract_wa_numbers(html)
    extract.widget_numbers = extract_widget_numbers(tree)
    extract.tel_numbers = extract_tel_numbers(tree)
    extract.jsonld = extract_jsonld(tree)
    extract.facebook_url, extract.instagram_url = extract_socials(tree, url)
    extract.crawl_targets = find_crawl_targets(tree, url)

    extract.text = visible_text(tree)
    extract.text_phones = extract_phones(extract.text)
    extract.emails = extract_emails(tree, extract.text)

    jsonld_numbers: list[ParsedPhone] = []
    seen: set[str] = set()
    for block in extract.jsonld:
        for telephone in block.telephones:
            parsed = normalise(telephone)
            if parsed and parsed.e164 not in seen:
                seen.add(parsed.e164)
                jsonld_numbers.append(parsed)
        for email in block.emails:
            candidate = email.strip().lower()
            if _is_plausible_email(candidate) and candidate not in extract.emails:
                extract.emails.append(candidate)
    extract.jsonld_numbers = jsonld_numbers

    if not extract.facebook_url or not extract.instagram_url:
        _fill_socials_from_jsonld(extract)

    return extract


def _fill_socials_from_jsonld(extract: PageExtract) -> None:
    """``sameAs`` is where a well-marked-up site lists its profiles."""
    for block in extract.jsonld:
        for url in block.same_as:
            host = registrable_domain(url) or ""
            if not extract.facebook_url and host.endswith(_FACEBOOK_HOSTS):
                extract.facebook_url = url
            elif not extract.instagram_url and host.endswith(_INSTAGRAM_HOSTS):
                extract.instagram_url = url

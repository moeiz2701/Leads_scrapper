"""Text normalisation for matching (§10.1).

``name_norm`` is what the fuzzy dedupe compares, so what gets stripped here
decides what counts as the same business. The bias is deliberate: strip
decoration (punctuation, legal suffixes, city tags, the word "salon" when it is
boilerplate) but never strip distinguishing words, because a false merge silently
destroys a lead while a false split only costs a duplicate row.
"""

from __future__ import annotations

import re
import unicodedata

# Legal and boilerplate suffixes that carry no identity.
_SUFFIXES = (
    "pvt ltd", "private limited", "pvt limited", "ltd", "limited", "llc", "inc",
    "co", "company", "and sons", "& sons", "and co", "& co",
)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalise_name(name: str) -> str:
    """Casefold, strip accents and punctuation, collapse whitespace, drop suffixes."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()

    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -len(suffix) - 1].strip()
                changed = True
    return text


def registrable_domain(url: str) -> str | None:
    """Host without ``www``. §10.1's domain-match dedupe key.

    Not a public-suffix-list implementation — for the PK SMB sites in scope
    (``.pk``, ``.com.pk``, ``.com``) host-minus-www is the right granularity, and
    a full PSL dependency would buy nothing.
    """
    from urllib.parse import urlsplit

    if not url:
        return None
    candidate = url if "//" in url else f"//{url}"
    host = urlsplit(candidate).netloc.lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host or None


# Social hosts get routed to their own columns rather than ``website`` — for PK
# SMBs an Instagram profile in the website slot is extremely common, and it is a
# §6.4 bio-link lead, not a §5.2 website-module lead.
_INSTAGRAM_HOSTS = {"instagram.com", "instagr.am"}
_FACEBOOK_HOSTS = {"facebook.com", "fb.com", "fb.me", "m.facebook.com"}


def classify_site_url(url: str | None) -> tuple[str | None, str | None, str | None]:
    """Split a URL into ``(website, facebook_url, instagram_url)``."""
    if not url:
        return None, None, None
    domain = registrable_domain(url) or ""
    if domain in _INSTAGRAM_HOSTS:
        return None, None, url
    if domain in _FACEBOOK_HOSTS:
        return None, url, None
    return url, None, None

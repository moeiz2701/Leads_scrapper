"""Pakistani phone extraction, normalisation and classification (§9.1, §9.2).

Design: **permissive extraction, strict validation.** A regex tight enough to
only match real numbers also misses half the formats in the wild, so we cast a
wide net, drop obvious non-phones (CNIC, NTN, prices) by context, and let
libphonenumber be the arbiter of what is actually a valid PK number.

A note on §9.1's published regex::

    (?:(?:\\+|00)?92[\\s\\-.]?|0)(3\\d{2}|\\d{2,3})[\\s\\-.]?\\d{6,8}

It does not match three of the eight formats the same section lists as verified
in live source data — ``(92 42) 35772057`` and ``(021) 111 339 339`` because of
the parentheses, and ``042 111 117 638`` because the trailing ``\\d{6,8}`` cannot
span two separators. The grouped-run pattern below covers all eight; §9.1's
regex is kept in the test suite as a documented baseline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberType

from leadscraper.enums import LineType

REGION = "PK"

# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

# A candidate is: optional "(", optional +/00, the 92 country code or a trunk 0,
# then one to five further digit groups separated by at most two punctuation
# characters. Deliberately loose — validation happens downstream.
_CANDIDATE_RE = re.compile(
    r"""
    (?<![0-9])                      # not mid-number
    \(?                             # optional opening paren:  (92 42) ...
    (?:(?:\+|00)\s?)?               # optional international prefix
    (?:92|0)                        # country code, or national trunk prefix
    (?:[\s\-. )]{0,2}\d{2,8}){1,5}
    """,
    re.VERBOSE,
)

# §9.1 "reject before normalising". CNIC is the dangerous one: 13 digits that
# often sit next to a phone number on the same contact page.
_CNIC_RE = re.compile(r"\b\d{5}[-\s]?\d{7}[-\s]?\d\b")

# Words that, appearing just before a digit run, mean it is not a phone number.
_POISON_PREFIX_RE = re.compile(
    r"(?:rs\.?|pkr|₨|rupees|price|cnic|nic|ntn|n\.t\.n|stn|reg(?:\.|istration)?"
    r"|plot|shop|suite|flat|invoice|order|account|a/c|iban|gst|ref|str?eet)"
    r"[\s.\-]{0,2}(?:no\.?|num(?:ber)?|#)?[\s:#.\-]{0,4}$",
    re.IGNORECASE,
)

# Trailing currency marker: "35,000/-"
_POISON_SUFFIX_RE = re.compile(r"^\s*(?:/-|/=|rs\.?\b|pkr\b|₨)", re.IGNORECASE)

_CONTEXT_WINDOW = 16


@dataclass(frozen=True, slots=True)
class ParsedPhone:
    """A validated PK number plus everything downstream stages need."""

    raw: str
    e164: str
    national: str
    line_type: LineType
    operator: str | None
    span: tuple[int, int]

    @property
    def is_mobile(self) -> bool:
        return self.line_type is LineType.MOBILE


# --------------------------------------------------------------------------- #
# Classification (§9.2)
# --------------------------------------------------------------------------- #

# Keyed on the first two digits of the national significant number (leading 0
# stripped), i.e. 0300-xxxxxxx -> "30".
_OPERATORS: dict[str, str] = {
    "30": "Jazz / Mobilink",
    "31": "Zong",
    "32": "Warid (Jazz)",
    "33": "Ufone",
    "34": "Telenor",
    "35": "SCO",
}

# §9.2 landline area codes, as national significant prefixes (no trunk 0).
_CITY_CODES: dict[str, str] = {
    "21": "Karachi",
    "42": "Lahore",
    "51": "Islamabad / Rawalpindi",
    "41": "Faisalabad",
    "61": "Multan",
    "91": "Peshawar",
    "55": "Gujranwala",
    "52": "Sialkot",
    "22": "Hyderabad",
    "81": "Quetta",
}


def classify(national: str) -> tuple[LineType, str | None]:
    """Map a national significant number to its line type and operator.

    ``national`` has no leading zero: ``3001234567``, ``4235772057``.
    """
    if not national.isdigit():
        return LineType.UNKNOWN, None

    if national.startswith("800"):
        return LineType.TOLL_FREE, None

    # UAN: <area code><111><6 digits>. Area codes here are 2 or 3 digits.
    for area_len in (2, 3):
        if national[area_len : area_len + 3] == "111" and len(national) == area_len + 9:
            return LineType.UAN, _CITY_CODES.get(national[:area_len])

    if national.startswith("3") and len(national) == 10:
        return LineType.MOBILE, _OPERATORS.get(national[:2])

    if national[:2] in _CITY_CODES:
        return LineType.LANDLINE, _CITY_CODES[national[:2]]

    return LineType.UNKNOWN, None


def _line_type_from_libphonenumber(num: phonenumbers.PhoneNumber) -> LineType:
    """Fallback when our prefix table does not recognise the number."""
    match phonenumbers.number_type(num):
        case PhoneNumberType.MOBILE:
            return LineType.MOBILE
        case PhoneNumberType.FIXED_LINE | PhoneNumberType.FIXED_LINE_OR_MOBILE:
            return LineType.LANDLINE
        case PhoneNumberType.TOLL_FREE:
            return LineType.TOLL_FREE
        case _:
            return LineType.UNKNOWN


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


_INTL_PREFIX_RE = re.compile(r"^\s*[(]?\s*(?:\+|00)")
_DIGIT_RUN_RE = re.compile(r"\d+")


def iter_numbers(raw: str, offset: int = 0):
    """Yield every valid PK number inside one candidate string, left to right.

    Contact pages routinely print two numbers with nothing but a space between
    them — ``0300-1234567 0321-7654321``. The candidate regex swallows both, so
    we walk the digit groups: longest valid run wins, then we resume from the
    group after it. Trimming only from the right would silently drop the second
    number, which is the more likely mobile of the pair on a salon listing.
    """
    groups = list(_DIGIT_RUN_RE.finditer(raw))
    had_intl = bool(_INTL_PREFIX_RE.match(raw))

    i = 0
    while i < len(groups):
        for j in range(len(groups), i, -1):
            digits = "".join(g.group(0) for g in groups[i:j])
            if not 9 <= len(digits) <= 13:
                continue
            parsed = _parse_valid(_to_dialable(digits, had_intl and i == 0))
            if parsed is None:
                continue
            span = (offset + groups[i].start(), offset + groups[j - 1].end())
            yield _build(parsed, raw[groups[i].start() : groups[j - 1].end()], span)
            i = j
            break
        else:
            i += 1


def _build(parsed: phonenumbers.PhoneNumber, raw: str, span: tuple[int, int]) -> ParsedPhone:
    national = str(parsed.national_number)
    line_type, operator = classify(national)
    if line_type is LineType.UNKNOWN:
        line_type = _line_type_from_libphonenumber(parsed)
    return ParsedPhone(
        raw=raw.strip(),
        e164=phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164),
        national=national,
        line_type=line_type,
        operator=operator,
        span=span,
    )


def normalise(raw: str, span: tuple[int, int] | None = None) -> ParsedPhone | None:
    """Parse one raw string into E.164 + classification, or ``None`` if invalid."""
    for phone in iter_numbers(raw):
        if span is not None:
            return ParsedPhone(
                raw=phone.raw,
                e164=phone.e164,
                national=phone.national,
                line_type=phone.line_type,
                operator=phone.operator,
                span=span,
            )
        return phone
    return None


def _to_dialable(digits: str, had_intl: bool) -> str:
    """Rebuild a string libphonenumber can parse with region PK."""
    if digits.startswith("92") and (had_intl or len(digits) >= 11):
        return "+" + digits
    if digits.startswith("0"):
        return digits
    # Bare national significant number, e.g. "3001234567".
    return "0" + digits


def _parse_valid(candidate: str) -> phonenumbers.PhoneNumber | None:
    try:
        parsed = phonenumbers.parse(candidate, REGION)
    except NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    if phonenumbers.region_code_for_number(parsed) != REGION:
        return None
    return parsed


# --------------------------------------------------------------------------- #
# Rejection (§9.1)
# --------------------------------------------------------------------------- #


def _rejected(text: str, start: int, end: int, cnic_spans: list[tuple[int, int]]) -> bool:
    for c_start, c_end in cnic_spans:
        if start < c_end and c_start < end:
            return True

    before = text[max(0, start - _CONTEXT_WINDOW) : start]
    if _POISON_PREFIX_RE.search(before):
        return True

    after = text[end : end + 6]
    if _POISON_SUFFIX_RE.search(after):
        return True

    # A bare 13-digit *contiguous* run is a CNIC with its dashes stripped.
    # Measured per run, not summed across the whole candidate — summing rejects
    # legitimate "0300-1234567 0321-7654321" pairs, which are common in listings.
    return any(len(run) >= 13 for run in _DIGIT_RUN_RE.findall(text[start:end]))


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def extract_phones(text: str) -> list[ParsedPhone]:
    """Extract every valid, non-rejected PK number from free text.

    Deduplicated on E.164, preserving first-seen order so the earliest (usually
    most prominent) occurrence keeps its position for §9.3 proximity scoring.
    """
    if not text:
        return []

    cnic_spans = [m.span() for m in _CNIC_RE.finditer(text)]
    seen: set[str] = set()
    results: list[ParsedPhone] = []

    for match in _CANDIDATE_RE.finditer(text):
        raw = match.group(0).rstrip(" \t.- )")
        start, end = match.start(), match.start() + len(raw)
        if _rejected(text, start, end, cnic_spans):
            continue
        for phone in iter_numbers(raw, offset=start):
            if phone.e164 in seen:
                continue
            seen.add(phone.e164)
            results.append(phone)

    return results


def to_e164(raw: str) -> str | None:
    """Convenience wrapper: one raw string in, E.164 or ``None`` out."""
    parsed = normalise(raw)
    return parsed.e164 if parsed else None

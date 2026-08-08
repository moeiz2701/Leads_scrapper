"""§9.1 / §9.2 — extraction, rejection and classification.

The eight formats in ``VERIFIED_FORMATS`` are the ones implementation.md §9.1
lists as observed in live source data. They are the contract: if a change breaks
one of these, it breaks real pages.
"""

from __future__ import annotations

import re

import pytest

from leadscraper.core.phone import (
    classify,
    extract_phones,
    normalise,
    to_e164,
)
from leadscraper.enums import LineType

# (raw, expected E.164, expected line type)
# Note on the UAN entries: a PK UAN has an 11-digit national significant number
# (area code + 111 + 6 digits), so 042-111-117-638 is +9242111117638. Confirmed
# against libphonenumber, which validates it.
VERIFIED_FORMATS = [
    ("0300-1234567", "+923001234567", LineType.MOBILE),
    ("+92 300 1234567", "+923001234567", LineType.MOBILE),
    ("03001234567", "+923001234567", LineType.MOBILE),
    ("(92 42) 35772057", "+924235772057", LineType.LANDLINE),
    ("+92-42-35771025", "+924235771025", LineType.LANDLINE),
    ("042 111 117 638", "+9242111117638", LineType.UAN),
    ("(021) 111 339 339", "+9221111339339", LineType.UAN),
    ("92 52 3258881", "+92523258881", LineType.LANDLINE),
]


@pytest.mark.parametrize(("raw", "expected", "line_type"), VERIFIED_FORMATS)
def test_verified_formats_round_trip(raw: str, expected: str, line_type: LineType) -> None:
    found = extract_phones(raw)
    assert [p.e164 for p in found] == [expected]
    assert found[0].line_type is line_type


def test_published_regex_is_insufficient_for_its_own_examples() -> None:
    """Documents why phone.py does not use §9.1's literal regex.

    Three of the eight formats §9.1 itself lists as verified do not match the
    regex §9.1 publishes. This test pins that fact so the divergence is a
    recorded decision rather than something a future reader "fixes" back.
    """
    published = re.compile(r"(?:(?:\+|00)?92[\s\-.]?|0)(3\d{2}|\d{2,3})[\s\-.]?\d{6,8}")
    unmatched = [raw for raw, _, _ in VERIFIED_FORMATS if not published.fullmatch(raw)]
    assert set(unmatched) == {
        "(92 42) 35772057",
        "042 111 117 638",
        "(021) 111 339 339",
    }


# --------------------------------------------------------------------------- #
# Run-on numbers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "0300-1234567 0321-7654321",
        "0300 1234567 0321 7654321",
        "Call 0300-1234567 or 0321-7654321 today",
        "0300-1234567 / 0321-7654321",
        "Ph: 0300-1234567, Mob: 0321-7654321",
    ],
)
def test_adjacent_numbers_both_recovered(text: str) -> None:
    """Contact pages print two numbers with only a space between them.

    Trimming a candidate only from the right would return the first and silently
    drop the second — often the mobile, which is the one that matters.
    """
    assert [p.e164 for p in extract_phones(text)] == ["+923001234567", "+923217654321"]


# --------------------------------------------------------------------------- #
# §9.1 "reject before normalising"
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("CNIC 35202-1234567-1 and phone 0333 4455667", ["+923334455667"]),
        ("35202-1234567-1", []),
        ("3520212345671", []),
        ("Price Rs 35,000/- only. Contact 0345-1112233", ["+923451112233"]),
        ("Rs 0300123456", []),
        ("NTN 1234567-8, mobile +92 321 4567890", ["+923214567890"]),
        ("Plot No 042 111 117 638", []),
        ("Shop # 0300 1234567", []),
        ("Year 2024 revenue 1234567890", []),
        ("A/C 0301 2345678901234", []),
        ("", []),
        ("no numbers here at all", []),
    ],
)
def test_rejections(text: str, expected: list[str]) -> None:
    assert [p.e164 for p in extract_phones(text)] == expected


def test_duplicates_collapse_preserving_first_position() -> None:
    text = "Call 0300-1234567. Or WhatsApp 0300 1234567. Landline 042-35771025."
    found = extract_phones(text)
    assert [p.e164 for p in found] == ["+923001234567", "+924235771025"]
    assert found[0].span[0] < found[1].span[0]


# --------------------------------------------------------------------------- #
# §9.2 classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("national", "line_type", "operator"),
    [
        ("3001234567", LineType.MOBILE, "Jazz / Mobilink"),
        ("3101234567", LineType.MOBILE, "Zong"),
        ("3201234567", LineType.MOBILE, "Warid (Jazz)"),
        ("3301234567", LineType.MOBILE, "Ufone"),
        ("3401234567", LineType.MOBILE, "Telenor"),
        ("3551234567", LineType.MOBILE, "SCO"),
        ("2135772057", LineType.LANDLINE, "Karachi"),
        ("4235772057", LineType.LANDLINE, "Lahore"),
        ("5135772057", LineType.LANDLINE, "Islamabad / Rawalpindi"),
        ("5535772057", LineType.LANDLINE, "Gujranwala"),
        ("42111117638", LineType.UAN, "Lahore"),
        ("21111339339", LineType.UAN, "Karachi"),
        ("80012345", LineType.TOLL_FREE, None),
    ],
)
def test_classify(national: str, line_type: LineType, operator: str | None) -> None:
    assert classify(national) == (line_type, operator)


@pytest.mark.parametrize("raw", ["0300-1234567", "0310-1234567", "0320-1234567",
                                 "0330-1234567", "0340-1234567", "0355-1234567"])
def test_every_documented_mobile_prefix_is_mobile(raw: str) -> None:
    """§9.2 lists 030x–035x as mobile; all must classify as such, since the
    WhatsApp-likelihood floor in §9.3 keys off ``line_type``.

    One representative per operator, not every 03x0 — §9.2's "035x" is broader
    than the real allocation. Only 0355 is assigned (SCO, for AJK and
    Gilgit-Baltistan); 0350 is unallocated and libphonenumber rejects it, which
    is correct behaviour we want to preserve.
    """
    parsed = normalise(raw)
    assert parsed is not None
    assert parsed.is_mobile
    assert parsed.operator is not None


def test_landline_is_never_mobile() -> None:
    for raw in ("042-35771025", "021-35772057", "051-2345678"):
        parsed = normalise(raw)
        assert parsed is not None and not parsed.is_mobile


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #


def test_to_e164_helper() -> None:
    assert to_e164("0300-1234567") == "+923001234567"
    assert to_e164("not a number") is None


def test_spans_index_into_the_source_text() -> None:
    """§9.3 proximity scoring reads a window around the number, so the span must
    point at the number's real position in the original string, not the candidate."""
    text = "Contact us on WhatsApp: 0300-1234567 anytime"
    phone = extract_phones(text)[0]
    start, end = phone.span
    assert text[start:end].replace("-", "").replace(" ", "") == "03001234567"


def test_non_pk_numbers_are_rejected() -> None:
    assert extract_phones("+1 415 555 2671") == []
    assert extract_phones("+44 20 7946 0958") == []


def test_tel_href_value() -> None:
    assert to_e164("tel:+923001234567") == "+923001234567"

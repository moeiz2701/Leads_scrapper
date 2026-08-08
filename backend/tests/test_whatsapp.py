"""§9.3 — WhatsApp evidence scoring.

The rule this file exists to protect: we never test whether a number has
WhatsApp, we score the evidence the public web already published. Nothing here
should ever grow a network call.
"""

from __future__ import annotations

import pytest

from leadscraper.core.whatsapp import (
    SIGNAL_SCORES,
    WaSignal,
    baseline_signal,
    evaluate,
    extract_wa_numbers,
    label_for,
    mentions_whatsapp_near,
    score_signals,
)
from leadscraper.enums import LineType, WhatsAppLabel


@pytest.mark.parametrize(
    ("signal", "score", "label"),
    [
        (WaSignal.WA_ME_LINK, 1.00, WhatsAppLabel.CONFIRMED),
        (WaSignal.CHAT_WIDGET, 0.95, WhatsAppLabel.CONFIRMED),
        (WaSignal.PLATFORM_BUTTON, 0.90, WhatsAppLabel.CONFIRMED),
        (WaSignal.TEXT_PROXIMITY, 0.75, WhatsAppLabel.LIKELY),
        (WaSignal.MOBILE_ONLY, 0.60, WhatsAppLabel.LIKELY),
        (WaSignal.NONE, 0.00, WhatsAppLabel.NO),
    ],
)
def test_signal_table_matches_the_doc(signal, score, label) -> None:
    assert SIGNAL_SCORES[signal] == score
    assert label_for(score) is label


def test_strongest_signal_wins_and_evidence_does_not_accumulate() -> None:
    """A wa.me link plus a nearby mention is not stronger than the link alone."""
    both = score_signals([WaSignal.WA_ME_LINK, WaSignal.TEXT_PROXIMITY, WaSignal.MOBILE_ONLY])
    assert both.score == 1.00
    assert both.signal is WaSignal.WA_ME_LINK


def test_empty_signals_is_no_not_a_crash() -> None:
    assert score_signals([]).label is WhatsAppLabel.NO


@pytest.mark.parametrize(
    ("line_type", "signal"),
    [
        (LineType.MOBILE, WaSignal.MOBILE_ONLY),
        (LineType.LANDLINE, WaSignal.NONE),
        (LineType.UAN, WaSignal.NONE),
        (LineType.UNKNOWN, WaSignal.NONE),
    ],
)
def test_baseline_by_line_type(line_type, signal) -> None:
    assert baseline_signal(line_type) is signal


# --------------------------------------------------------------------------- #
# Link extraction — the highest-confidence signal in the system (§5.2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "markup",
    [
        '<a href="https://wa.me/923001234567">Chat</a>',
        '<a href="https://wa.me/+923001234567">Chat</a>',
        '<a href="//wa.me/923001234567">Chat</a>',
        '<a href="https://api.whatsapp.com/send?phone=923001234567&text=Hi">Chat</a>',
        '<a href="https://web.whatsapp.com/send?phone=923001234567">Chat</a>',
        "<a href='whatsapp://send?phone=923001234567'>Chat</a>",
    ],
)
def test_extract_wa_numbers_covers_every_link_shape(markup: str) -> None:
    assert extract_wa_numbers(markup) == ["+923001234567"]


def test_extract_wa_numbers_dedupes_and_orders() -> None:
    markup = (
        '<a href="https://wa.me/923001234567">a</a>'
        '<a href="https://api.whatsapp.com/send?phone=923217654321">b</a>'
        '<a href="https://wa.me/923001234567">c</a>'
    )
    assert extract_wa_numbers(markup) == ["+923001234567", "+923217654321"]


def test_extract_wa_numbers_ignores_junk() -> None:
    assert extract_wa_numbers("") == []
    assert extract_wa_numbers('<a href="https://example.com">nope</a>') == []
    # A non-PK number in a wa.me link is still not a PK lead.
    assert extract_wa_numbers('<a href="https://wa.me/14155552671">us</a>') == []


# --------------------------------------------------------------------------- #
# Proximity
# --------------------------------------------------------------------------- #


def test_mentions_whatsapp_near_respects_the_50_char_window() -> None:
    near = "WhatsApp us on 0300-1234567"
    span = (near.index("0300"), len(near))
    assert mentions_whatsapp_near(near, span)

    far = "WhatsApp" + " " * 80 + "0300-1234567"
    span_far = (far.index("0300"), len(far))
    assert not mentions_whatsapp_near(far, span_far)


def test_mentions_whatsapp_is_case_and_spacing_tolerant() -> None:
    for text in ("whatsapp 0300-1234567", "Whats App 0300-1234567", "WHATSAPP: 0300-1234567"):
        assert mentions_whatsapp_near(text, (text.index("0300"), len(text)))


# --------------------------------------------------------------------------- #
# End to end on one page
# --------------------------------------------------------------------------- #


def test_evaluate_confirms_a_number_with_a_wa_link() -> None:
    text = "Call 0300-1234567"
    ev = evaluate(
        text=text,
        phone_e164="+923001234567",
        phone_span=(5, len(text)),
        line_type=LineType.MOBILE,
        wa_numbers=["+923001234567"],
    )
    assert ev.is_confirmed
    assert ev.score == 1.00


def test_evaluate_landline_with_no_signal_scores_zero() -> None:
    text = "Office: 042-35771025"
    ev = evaluate(
        text=text,
        phone_e164="+924235771025",
        phone_span=(8, len(text)),
        line_type=LineType.LANDLINE,
    )
    assert ev.label is WhatsAppLabel.NO
    assert ev.score == 0.00


def test_evaluate_mobile_with_no_signal_is_likely_not_confirmed() -> None:
    text = "Call 0300-1234567"
    ev = evaluate(
        text=text,
        phone_e164="+923001234567",
        phone_span=(5, len(text)),
        line_type=LineType.MOBILE,
    )
    assert ev.label is WhatsAppLabel.LIKELY
    assert not ev.is_confirmed


def test_a_wa_link_for_a_different_number_does_not_confirm_this_one() -> None:
    """A page can carry one business's wa.me link and another's tel: number.
    Confirmation must key on the number itself, never on 'the page had a link'."""
    text = "Sales 0321-7654321"
    ev = evaluate(
        text=text,
        phone_e164="+923217654321",
        phone_span=(6, len(text)),
        line_type=LineType.MOBILE,
        wa_numbers=["+923001234567"],
    )
    assert not ev.is_confirmed

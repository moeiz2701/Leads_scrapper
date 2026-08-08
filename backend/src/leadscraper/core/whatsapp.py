"""WhatsApp evidence scoring (§9.3).

There is no legitimate API that answers "does this number have WhatsApp".
Automating WhatsApp Web to probe contacts violates WhatsApp's terms and gets the
probing number banned within days, so this module never tests a number — it
scores the *evidence* the public web already gives us, and exports a label.

The score stays internal; §9.3 is explicit that the operator sees the label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from leadscraper.core.phone import normalise
from leadscraper.enums import LineType, WhatsAppLabel


class WaSignal(StrEnum):
    """Evidence types, exactly as enumerated in the §9.3 table."""

    WA_ME_LINK = "wa_me_link"
    CHAT_WIDGET = "chat_widget"
    PLATFORM_BUTTON = "platform_button"
    TEXT_PROXIMITY = "text_proximity"
    MOBILE_ONLY = "mobile_only"
    NONE = "none"


SIGNAL_SCORES: dict[WaSignal, float] = {
    WaSignal.WA_ME_LINK: 1.00,       # wa.me / api.whatsapp.com/send?phone=
    WaSignal.CHAT_WIDGET: 0.95,      # widget with a matching data-phone
    WaSignal.PLATFORM_BUTTON: 0.90,  # FB Page / IG WhatsApp action
    WaSignal.TEXT_PROXIMITY: 0.75,   # "WhatsApp" within 50 chars of the number
    WaSignal.MOBILE_ONLY: 0.60,      # 03xx mobile, no other signal
    WaSignal.NONE: 0.00,             # landline / UAN
}

CONFIRMED_THRESHOLD = 0.90
LIKELY_THRESHOLD = 0.60

PROXIMITY_WINDOW = 50

_WHATSAPP_WORD_RE = re.compile(r"whats\s?app|واٹس\s?ایپ|wa\b", re.IGNORECASE)

# Every shape a WhatsApp click-to-chat link takes in the wild.
_WA_LINK_RE = re.compile(
    r"""(?:
        (?:https?:)?//(?:api\.whatsapp\.com|web\.whatsapp\.com)/send/?\?[^"'\s]*?phone=(\+?\d{9,15})
      | (?:https?:)?//wa\.me/(\+?\d{9,15})
      | whatsapp://send/?\?[^"'\s]*?phone=(\+?\d{9,15})
    )""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class WaEvidence:
    score: float
    label: WhatsAppLabel
    signal: WaSignal

    @property
    def is_confirmed(self) -> bool:
        return self.label is WhatsAppLabel.CONFIRMED


def label_for(score: float) -> WhatsAppLabel:
    if score >= CONFIRMED_THRESHOLD:
        return WhatsAppLabel.CONFIRMED
    if score >= LIKELY_THRESHOLD:
        return WhatsAppLabel.LIKELY
    return WhatsAppLabel.NO


def score_signals(signals: set[WaSignal] | list[WaSignal]) -> WaEvidence:
    """Take the strongest signal present. Evidence does not accumulate.

    A number with both a wa.me link and a nearby "WhatsApp" mention is not more
    confirmed than one with the link alone — the link already settles it.
    """
    if not signals:
        return WaEvidence(0.0, WhatsAppLabel.NO, WaSignal.NONE)
    best = max(signals, key=lambda s: SIGNAL_SCORES.get(s, 0.0))
    score = SIGNAL_SCORES.get(best, 0.0)
    return WaEvidence(score=score, label=label_for(score), signal=best)


def baseline_signal(line_type: LineType) -> WaSignal:
    """The floor for a number with no positive evidence either way (§9.3)."""
    return WaSignal.MOBILE_ONLY if line_type is LineType.MOBILE else WaSignal.NONE


def extract_wa_numbers(html_or_text: str) -> list[str]:
    """Pull every WhatsApp click-to-chat number out of markup, as E.164.

    This is the highest-confidence extraction in the entire system (§5.2) — a
    business publishing a wa.me link is telling you the number takes WhatsApp.
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _WA_LINK_RE.finditer(html_or_text or ""):
        digits = next((g for g in match.groups() if g), None)
        if not digits:
            continue
        parsed = normalise(digits if digits.startswith("+") else "+" + digits.lstrip("+"))
        if parsed and parsed.e164 not in seen:
            seen.add(parsed.e164)
            found.append(parsed.e164)
    return found


def mentions_whatsapp_near(
    text: str, span: tuple[int, int], window: int = PROXIMITY_WINDOW
) -> bool:
    """§9.3: the word "WhatsApp" within ``window`` characters of the number."""
    start, end = span
    context = text[max(0, start - window) : min(len(text), end + window)]
    return bool(_WHATSAPP_WORD_RE.search(context))


def evaluate(
    text: str,
    phone_e164: str,
    phone_span: tuple[int, int],
    line_type: LineType,
    wa_numbers: list[str] | None = None,
    widget_numbers: list[str] | None = None,
    platform_button_numbers: list[str] | None = None,
) -> WaEvidence:
    """Score one number against every signal available on one page."""
    signals: set[WaSignal] = {baseline_signal(line_type)}

    if phone_e164 in (wa_numbers or []):
        signals.add(WaSignal.WA_ME_LINK)
    if phone_e164 in (widget_numbers or []):
        signals.add(WaSignal.CHAT_WIDGET)
    if phone_e164 in (platform_button_numbers or []):
        signals.add(WaSignal.PLATFORM_BUTTON)
    if mentions_whatsapp_near(text, phone_span):
        signals.add(WaSignal.TEXT_PROXIMITY)

    return score_signals(signals)

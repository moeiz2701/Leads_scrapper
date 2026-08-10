"""§3.3 ranking of a business's contact set by ``number_preference``. Pure.

§3.3 is explicit that this "controls **ranking**, not filtering (except
``whatsapp_only``)", and §12.1 consumes the result as four ranked phone slots.

| Preference | Order |
|---|---|
| ``owner_first`` | named-person number → mobile w/ WA confirmed → mobile → landline |
| ``business_first`` | main business line → mobile w/ WA confirmed → named-person → landline |
| ``whatsapp_only`` | filter out everything without WhatsApp evidence, then rank by confidence |

**Filtering is expressed as ``rank = None``, never as a delete.** ``whatsapp_only``
genuinely excludes numbers, but §10.1's "never discard a contact" and §15's
deletion/provenance requirements both mean the row stays — it simply does not
occupy an export slot. Changing the preference and re-ranking is then lossless.

The functions here read the ``contacts`` column names directly (see
``RankableContact``) so the stage can sort ORM rows without a mapping layer, and
tests can sort a five-line stub.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from leadscraper.enums import BelongsTo, LineType, NumberPreference, Source, WhatsAppLabel


class RankableContact(Protocol):
    """The subset of a ``contacts`` row §3.3 ranks on.

    Read-only members, so a ``Contact``'s narrower column types (``Decimal |
    None``) satisfy the wider ones accepted here. Declared as mutable attributes
    they would be invariant and no ORM row would match.
    """

    @property
    def line_type(self) -> str | None: ...
    @property
    def wa_evidence(self) -> Decimal | float | None: ...
    @property
    def wa_label(self) -> str | None: ...
    @property
    def confidence(self) -> Decimal | float | None: ...
    @property
    def person_name(self) -> str | None: ...
    @property
    def belongs_to(self) -> str | None: ...
    @property
    def source(self) -> str: ...
    @property
    def value_e164(self) -> str | None: ...


# Tie-break inside a tier, after evidence and confidence. A mobile you can
# message beats a landline you can only call.
_LINE_TYPE_ORDER: dict[str, int] = {
    LineType.MOBILE: 0,
    LineType.UAN: 1,
    LineType.LANDLINE: 2,
    LineType.TOLL_FREE: 3,
    LineType.UNKNOWN: 4,
}

_UNRANKED = 9


def _score(contact: RankableContact) -> float:
    return float(contact.wa_evidence or 0.0)


def _confidence(contact: RankableContact) -> float:
    return float(contact.confidence or 0.0)


def _is_mobile(contact: RankableContact) -> bool:
    return contact.line_type == LineType.MOBILE


def _is_confirmed(contact: RankableContact) -> bool:
    return contact.wa_label == WhatsAppLabel.CONFIRMED


def has_whatsapp_evidence(contact: RankableContact) -> bool:
    """§3.3's ``whatsapp_only`` filter.

    Any non-zero §9.3 score counts, which keeps the ``likely`` band — a bare
    ``03xx`` scores 0.60 precisely because a PK mobile probably does take
    WhatsApp. Restricting this to ``confirmed`` would cut the Islamabad run from
    256 numbers to 53 and read to the operator as a broken run rather than as a
    filter they chose.
    """
    return _score(contact) > 0.0


def is_main_business_line(contact: RankableContact) -> bool:
    """``business_first``'s top tier — the number the business publishes as its own.

    Two shapes qualify: a UAN, which exists for no other purpose, and the number
    on the business's own Maps listing, which the business itself registered.
    §3.3 puts plain "landline" in the *bottom* tier of the same preference, so
    this tier cannot simply mean "not a mobile"; it means the primary published
    line, and everything else — including landlines found elsewhere — falls
    through to the residual tier.
    """
    if contact.belongs_to not in (None, BelongsTo.BUSINESS):
        return False
    return contact.line_type == LineType.UAN or contact.source == Source.GOOGLE_MAPS


def _tier_owner_first(contact: RankableContact) -> int:
    if contact.person_name:
        return 0
    if _is_mobile(contact) and _is_confirmed(contact):
        return 1
    if _is_mobile(contact):
        return 2
    return 3


def _tier_business_first(contact: RankableContact) -> int:
    if is_main_business_line(contact):
        return 0
    if _is_mobile(contact) and _is_confirmed(contact):
        return 1
    if contact.person_name:
        return 2
    return 3


def _tier(contact: RankableContact, preference: NumberPreference) -> int:
    match preference:
        case NumberPreference.OWNER_FIRST:
            return _tier_owner_first(contact)
        case NumberPreference.BUSINESS_FIRST:
            return _tier_business_first(contact)
        case _:
            # whatsapp_only: §3.3 says "rank by confidence" with no tiers. The
            # sort key below orders by §9.3 evidence and then by contact
            # confidence, which satisfies both readings of that phrase in the
            # order a caller who asked for WhatsApp-only would want them.
            return 0


def sort_key(contact: RankableContact, preference: NumberPreference) -> tuple:
    """Total order within a business's contact set. Deterministic to the E.164."""
    return (
        _tier(contact, preference),
        -_score(contact),
        -_confidence(contact),
        _LINE_TYPE_ORDER.get(contact.line_type or "", _UNRANKED),
        contact.value_e164 or "",
    )


def rank_contacts[C: RankableContact](
    contacts: Sequence[C], preference: NumberPreference
) -> list[tuple[C, int | None]]:
    """Assign §3.3 ranks. ``None`` means "not in the exported set".

    Pass phone contacts only — §12.1 has one ``email`` column and no ranked email
    slots, so an email's rank would be meaningless.

    A rank is given to at most one row per distinct number. Two rows can carry
    the same E.164 after a §10.1 merge folds two businesses together, and each is
    a real provenance record worth keeping (§1) — but the operator must not be
    handed the same number as both ``phone_1`` and ``phone_2``. The best-ordered
    row wins the slot and its duplicates go unranked, which loses no data because
    the rows are still there.
    """
    if preference is NumberPreference.WHATSAPP_ONLY:
        eligible = [c for c in contacts if has_whatsapp_evidence(c)]
        excluded: list[C] = [c for c in contacts if not has_whatsapp_evidence(c)]
    else:
        eligible, excluded = list(contacts), []

    eligible.sort(key=lambda c: sort_key(c, preference))

    ranked: list[tuple[C, int | None]] = []
    seen: set[str] = set()
    position = 0
    for contact in eligible:
        key = contact.value_e164 or ""
        if key and key in seen:
            ranked.append((contact, None))
            continue
        seen.add(key)
        position += 1
        ranked.append((contact, position))

    ranked.extend((contact, None) for contact in excluded)
    return ranked

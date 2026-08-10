"""§3.3 ranking by ``number_preference`` — pure, no DB."""

from __future__ import annotations

from dataclasses import dataclass

from leadscraper.core.ranking import (
    has_whatsapp_evidence,
    is_main_business_line,
    rank_contacts,
)
from leadscraper.enums import (
    BelongsTo,
    LineType,
    NumberPreference,
    Source,
    WhatsAppLabel,
)


@dataclass(eq=False)
class Stub:
    """A ``contacts`` row, reduced to what §3.3 reads. Same attribute names.

    ``eq=False`` keeps identity semantics, so two rows carrying the same number
    stay distinguishable — which is exactly the case the merge tests exercise.
    """

    value_e164: str
    line_type: str | None = LineType.MOBILE
    wa_evidence: float | None = 0.60
    wa_label: str | None = WhatsAppLabel.LIKELY
    confidence: float | None = 0.85
    person_name: str | None = None
    belongs_to: str | None = BelongsTo.BUSINESS
    source: str = Source.BUSINESS_WEBSITE


def owner(**kw) -> Stub:
    return Stub(person_name="Hina Khan", belongs_to=BelongsTo.OWNER, **kw)


def confirmed_mobile(**kw) -> Stub:
    return Stub(wa_evidence=1.00, wa_label=WhatsAppLabel.CONFIRMED, **kw)


def landline(**kw) -> Stub:
    return Stub(
        line_type=LineType.LANDLINE,
        wa_evidence=0.0,
        wa_label=WhatsAppLabel.NO,
        **kw,
    )


def order(contacts, preference) -> list[str]:
    ranked = [(r, c) for c, r in rank_contacts(contacts, preference) if r is not None]
    return [c.value_e164 for _, c in sorted(ranked, key=lambda pair: pair[0])]


def ranks(contacts, preference) -> dict[str, int | None]:
    return {c.value_e164: r for c, r in rank_contacts(contacts, preference)}


# --------------------------------------------------------------------------- #
# owner_first
# --------------------------------------------------------------------------- #


def test_owner_first_follows_the_documented_order() -> None:
    """§3.3: named-person → mobile w/ WA confirmed → mobile → landline."""
    contacts = [
        landline(value_e164="+924232294007"),
        Stub(value_e164="+923211885256"),
        confirmed_mobile(value_e164="+923001234567"),
        owner(value_e164="+923339876543"),
    ]
    assert order(contacts, NumberPreference.OWNER_FIRST) == [
        "+923339876543",  # named person
        "+923001234567",  # mobile, WhatsApp confirmed
        "+923211885256",  # mobile
        "+924232294007",  # landline
    ]


def test_owner_first_prefers_a_named_person_even_over_a_confirmed_mobile() -> None:
    """The whole point of the preference: §1's target output is a number
    attributed to an owner."""
    contacts = [
        confirmed_mobile(value_e164="+923001234567"),
        owner(value_e164="+923339876543", wa_evidence=0.60),
    ]
    assert order(contacts, NumberPreference.OWNER_FIRST)[0] == "+923339876543"


# --------------------------------------------------------------------------- #
# business_first
# --------------------------------------------------------------------------- #


def test_business_first_puts_the_listed_number_ahead_of_the_owners_cell() -> None:
    """§3.3: main business line → mobile w/ WA confirmed → named-person →
    landline. The operator asked for the shop, not the proprietor."""
    contacts = [
        owner(value_e164="+923339876543"),
        confirmed_mobile(value_e164="+923001234567"),
        Stub(value_e164="+924232294007", line_type=LineType.UAN, source=Source.GOOGLE_MAPS),
    ]
    assert order(contacts, NumberPreference.BUSINESS_FIRST) == [
        "+924232294007",
        "+923001234567",
        "+923339876543",
    ]


def test_main_business_line_is_the_uan_or_the_listing_number() -> None:
    """§3.3 puts plain "landline" in the *bottom* tier of business_first, so the
    top tier cannot just mean "not a mobile"."""
    assert is_main_business_line(
        Stub(value_e164="+924211117638", line_type=LineType.UAN)
    )
    assert is_main_business_line(
        Stub(value_e164="+924232294007", line_type=LineType.LANDLINE,
             source=Source.GOOGLE_MAPS)
    )
    # A landline scraped off the website is not the registered business line.
    assert not is_main_business_line(landline(value_e164="+924232294007"))
    # Nor is a number attributed to a person.
    assert not is_main_business_line(
        owner(value_e164="+923339876543", source=Source.GOOGLE_MAPS)
    )


# --------------------------------------------------------------------------- #
# whatsapp_only
# --------------------------------------------------------------------------- #


def test_whatsapp_only_excludes_by_unranking_rather_than_deleting() -> None:
    """§3.3 calls this a filter, but §10.1 says never discard a contact and §15
    needs the row for provenance. Excluded means ``rank = None``: the row is
    still there and switching the preference back is lossless."""
    contacts = [
        landline(value_e164="+924232294007"),
        confirmed_mobile(value_e164="+923001234567"),
    ]
    result = ranks(contacts, NumberPreference.WHATSAPP_ONLY)
    assert result["+923001234567"] == 1
    assert result["+924232294007"] is None
    assert len(rank_contacts(contacts, NumberPreference.WHATSAPP_ONLY)) == 2


def test_whatsapp_only_keeps_the_likely_band() -> None:
    """§9.3 scores a bare 03xx at 0.60/likely precisely because a PK mobile
    probably does take WhatsApp. Restricting the filter to `confirmed` would cut
    the Islamabad run from 256 numbers to 53 and read as a broken run."""
    assert has_whatsapp_evidence(Stub(value_e164="+923211885256"))
    assert not has_whatsapp_evidence(landline(value_e164="+924232294007"))


def test_whatsapp_only_ranks_the_strongest_evidence_first() -> None:
    contacts = [
        Stub(value_e164="+923211885256"),
        confirmed_mobile(value_e164="+923001234567"),
    ]
    assert order(contacts, NumberPreference.WHATSAPP_ONLY)[0] == "+923001234567"


# --------------------------------------------------------------------------- #
# Invariants that hold under every preference
# --------------------------------------------------------------------------- #


def test_ranks_are_dense_and_start_at_one() -> None:
    contacts = [Stub(value_e164=f"+92300123456{i}") for i in range(5)]
    for preference in NumberPreference:
        assigned = sorted(r for _, r in rank_contacts(contacts, preference) if r is not None)
        assert assigned == [1, 2, 3, 4, 5]


def test_one_number_takes_at_most_one_export_slot() -> None:
    """A §10.1 merge leaves two rows carrying the same number — each a real
    provenance record — and the operator must not be handed it as both phone_1
    and phone_2. The better-evidenced row wins the slot; the other keeps its row
    and loses only its rank."""
    from_maps = Stub(value_e164="+923001234567", source=Source.GOOGLE_MAPS)
    from_site = confirmed_mobile(value_e164="+923001234567")
    result = rank_contacts([from_maps, from_site], NumberPreference.OWNER_FIRST)

    assert sorted(r for _, r in result if r is not None) == [1]
    assert dict(result)[from_site] == 1
    assert dict(result)[from_maps] is None
    assert len(result) == 2


def test_ranking_is_deterministic_for_indistinguishable_contacts() -> None:
    a = Stub(value_e164="+923001234567")
    b = Stub(value_e164="+923009999999")
    forwards = order([a, b], NumberPreference.OWNER_FIRST)
    backwards = order([b, a], NumberPreference.OWNER_FIRST)
    assert forwards == backwards


def test_an_empty_contact_set_ranks_to_nothing() -> None:
    assert rank_contacts([], NumberPreference.OWNER_FIRST) == []

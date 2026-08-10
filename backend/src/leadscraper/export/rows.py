"""One business + its contacts → one §12.1 row. Pure; no DB, no I/O.

Every value here is either the real thing or ``None``. **``None`` means the
source never published it**, and it stays that way all the way to the cell —
§10.2's single load-bearing rule ("a field the source never published is not a
zero") does not stop at the scorer. A business with no ``review_count`` exports
blank, not ``0``; an unattributed business exports a blank ``contact_person``,
which §12.1 states outright: "never guessed".

The caller supplies the contacts. That is deliberate: suppression (§15) and the
§13 filters are query-layer concerns, and a row builder that re-applied them
would give the table and the CSV two chances to disagree. See
``services/results.py``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

from leadscraper.enums import ContactKind
from leadscraper.export.columns import PHONE_SLOTS, phone_slot_columns

# The canonical Google Maps permalink for a place. `businesses` stores the
# `place_id` and not a URL, because the URL is derivable and the id is the §10.1
# merge key — storing both would let them disagree.
_MAPS_PLACE_URL = "https://www.google.com/maps/place/?q=place_id:{place_id}"


class RowContact(Protocol):
    """The ``contacts`` columns §12.1 reads."""

    @property
    def kind(self) -> str: ...
    @property
    def value_e164(self) -> str | None: ...
    @property
    def value_raw(self) -> str: ...
    @property
    def line_type(self) -> str | None: ...
    @property
    def wa_label(self) -> str | None: ...
    @property
    def belongs_to(self) -> str | None: ...
    @property
    def person_name(self) -> str | None: ...
    @property
    def person_role(self) -> str | None: ...
    @property
    def attribution(self) -> str | None: ...
    @property
    def source(self) -> str: ...
    @property
    def source_url(self) -> str: ...
    @property
    def wa_evidence_url(self) -> str | None: ...
    @property
    def rank(self) -> int | None: ...
    @property
    def scraped_at(self) -> datetime | None: ...


def maps_url(place_id: str | None) -> str | None:
    return _MAPS_PLACE_URL.format(place_id=quote(place_id)) if place_id else None


def _ranked_phones(contacts: Iterable[RowContact]) -> list[RowContact]:
    """Phone contacts holding an export slot, in §3.3 rank order.

    ``rank is None`` covers two different facts and neither belongs in a column:
    ``whatsapp_only`` excluded the number, or a §10.1 merge left a second
    provenance row on a number that already has its slot (§3.3).
    """
    phones = [
        c for c in contacts if c.kind == ContactKind.PHONE and c.rank is not None
    ]
    phones.sort(key=lambda c: (c.rank or 0, c.value_e164 or ""))
    return phones


def _first_attributed(phones: Sequence[RowContact]) -> RowContact | None:
    """The best-ranked number that carries a name.

    §12.1 has one ``contact_person`` per business but attribution lives on the
    contact, so the three person columns are taken from **one** contact rather
    than assembled from the best of each — a name from one row and a role from
    another would be a join we never made (§8: "never fabricate the join").
    """
    return next((c for c in phones if c.person_name), None)


def _blank_to_none(value: str | None) -> str | None:
    return value or None


def _pipe(values: Iterable[str | None]) -> str | None:
    """§12.1 columns 39–40: pipe-delimited, de-duplicated, order preserved."""
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return "|".join(seen) if seen else None


def build_row(business: Any, contacts: Sequence[RowContact]) -> dict[str, Any]:
    """Project one business into the §12.1 column set.

    ``contacts`` is the business's *visible* contact set — already filtered for
    §15 suppression by the caller.
    """
    phones = _ranked_phones(contacts)
    emails = [c for c in contacts if c.kind == ContactKind.EMAIL]
    person = _first_attributed(phones)

    row: dict[str, Any] = {
        "business_name": business.name,
        "category": _blank_to_none(business.category),
        "subcategory": _blank_to_none(business.subcategory),
        "city": _blank_to_none(business.city),
        "area": _blank_to_none(business.area),
        "address": _blank_to_none(business.address),
        # Blank when unattributed — §12.1, and §8 has no engine until Phase 9, so
        # this is blank on all but one row in the live data. That is the honest
        # state of the record, not a gap to fill with the business name.
        "contact_person": person.person_name if person else None,
        "contact_role": person.person_role if person else None,
        "attribution": person.attribution if person else None,
        # Distinct numbers that hold a slot, which can exceed PHONE_SLOTS — the
        # operator needs to know a business has 7 numbers even though §12.1 shows
        # 4. Counted on the E.164 so a §10.1 merge's duplicate provenance rows do
        # not inflate it.
        "phone_count": len({c.value_e164 or c.value_raw for c in phones}),
    }

    for slot in range(1, PHONE_SLOTS + 1):
        value, type_, whatsapp, belongs_to, source = phone_slot_columns(slot)
        contact = phones[slot - 1] if slot <= len(phones) else None
        row[value] = (contact.value_e164 or contact.value_raw) if contact else None
        row[type_] = contact.line_type if contact else None
        # §9.3's *label*, never the raw evidence score — the operator sees
        # confirmed/likely/no and `wa_evidence` stays internal.
        row[whatsapp] = contact.wa_label if contact else None
        row[belongs_to] = contact.belongs_to if contact else None
        row[source] = contact.source if contact else None

    row["email"] = emails[0].value_raw if emails else None
    row["website"] = _blank_to_none(business.website)
    row["facebook_url"] = _blank_to_none(business.facebook_url)
    row["instagram_url"] = _blank_to_none(business.instagram_url)
    row["maps_url"] = maps_url(business.place_id)
    # Blank, not 0. A salon Maps never published a rating for is not a 0.0-rated
    # salon, and exporting it as one would be a lie the operator acts on.
    row["rating"] = float(business.rating) if business.rating is not None else None
    row["review_count"] = business.review_count
    row["lead_score"] = business.lead_score

    # Provenance spans *every* contact, not just the four with a column. §15's
    # deletion path and §1's "provenance survives everything" are about the
    # record, and the fifth number's source is as real as the first's.
    row["sources"] = _pipe(c.source for c in contacts)
    row["evidence_urls"] = _pipe(
        url
        for c in contacts
        # Both URLs, because they are routinely different pages: `source_url` is
        # where the value came from and `wa_evidence_url` is where the proof came
        # from (§5.2). Exporting only one drops half the provenance §1 requires.
        for url in (c.source_url, c.wa_evidence_url)
    )
    row["scraped_at"] = _scraped_at(business, contacts)
    return row


def _scraped_at(business: Any, contacts: Sequence[RowContact]) -> datetime | None:
    """§12.1 column 41 — how fresh this row is.

    The most recent evidence on the record, which is what "is this number still
    good?" actually turns on. Falls back to when the business was discovered,
    for a row whose contacts predate the column.
    """
    stamps = [c.scraped_at for c in contacts if c.scraped_at is not None]
    created = getattr(business, "created_at", None)
    if created is not None:
        stamps.append(created)
    return max(stamps) if stamps else None

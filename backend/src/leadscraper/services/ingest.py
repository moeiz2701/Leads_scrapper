"""Turning source results into ``businesses`` and ``contacts`` rows.

Two invariants this module exists to hold:

* **Cross-query dedupe on ``place_id``** (§10.1 strong match). The §5.1 fan-out
  deliberately runs overlapping queries — 12 tiles × 5 synonyms produces ~2,400
  raw hits for ~700 real businesses — so the same place arrives many times and
  must converge on one row.
* **Never discard a contact on merge** (§10.1). "A second number is a second
  column, and that's exactly what you asked for." Merging unions contacts and
  source URLs; it only ever overwrites a field that was empty.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from leadscraper.core.maps_payload import MapsResult
from leadscraper.core.phone import normalise
from leadscraper.core.textnorm import classify_site_url, normalise_name
from leadscraper.core.whatsapp import baseline_signal, score_signals
from leadscraper.db.models import Business, Contact
from leadscraper.enums import BelongsTo, ContactKind, Source


@dataclass(slots=True)
class IngestStats:
    created: int = 0
    merged: int = 0
    contacts_added: int = 0
    without_phone: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "created": self.created,
            "merged": self.merged,
            "contacts_added": self.contacts_added,
            "without_phone": self.without_phone,
        }


def ingest_maps_result(
    session: Session,
    run_id: uuid.UUID,
    result: MapsResult,
    *,
    area: str | None = None,
    city: str | None = None,
    category: str | None = None,
    stats: IngestStats | None = None,
) -> Business | None:
    """Upsert one Maps search result. Returns the business row, or ``None`` if
    the record carries no usable identity."""
    stats = stats or IngestStats()
    if not result.name and not result.place_id:
        return None

    business = _find_existing(session, run_id, result)
    website, facebook_url, instagram_url = classify_site_url(result.website)

    if business is None:
        business = Business(
            run_id=run_id,
            name=result.name or "",
            name_norm=normalise_name(result.name or ""),
            category=category,
            subcategory=result.category,
            city=city,
            area=area,
            address=result.address,
            lat=result.lat,
            lng=result.lng,
            place_id=result.place_id,
            website=website,
            facebook_url=facebook_url,
            instagram_url=instagram_url,
            rating=result.rating,
            review_count=result.review_count,
        )
        session.add(business)
        session.flush()
        stats.created += 1
    else:
        _merge_business(
            business,
            result,
            website=website,
            facebook_url=facebook_url,
            instagram_url=instagram_url,
            area=area,
        )
        stats.merged += 1

    if _add_phone_contact(session, business, result):
        stats.contacts_added += 1
    elif not result.phone_raw:
        stats.without_phone += 1

    return business


def _find_existing(session: Session, run_id: uuid.UUID, result: MapsResult) -> Business | None:
    """§10.1 tier 2 — ``place_id`` is the strong match and the only one Maps
    needs, since every Maps result carries one."""
    if not result.place_id:
        return None
    return session.execute(
        select(Business).where(
            Business.run_id == run_id, Business.place_id == result.place_id
        )
    ).scalar_one_or_none()


def _merge_business(
    business: Business,
    result: MapsResult,
    *,
    website: str | None,
    facebook_url: str | None,
    instagram_url: str | None,
    area: str | None,
) -> None:
    """Fill gaps only. A later query must never blank a field an earlier one set."""
    business.address = business.address or result.address
    business.website = business.website or website
    business.facebook_url = business.facebook_url or facebook_url
    business.instagram_url = business.instagram_url or instagram_url
    business.area = business.area or area
    if business.lat is None:
        business.lat = result.lat
    if business.lng is None:
        business.lng = result.lng
    if business.rating is None:
        business.rating = result.rating
    # §5.1: payload richness varies between responses, so a later hit may carry
    # the review count an earlier one omitted. Missing never overwrites present.
    if business.review_count is None and result.review_count is not None:
        business.review_count = result.review_count


def _add_phone_contact(session: Session, business: Business, result: MapsResult) -> bool:
    """Attach the payload phone, unless this business already has that number."""
    if not result.phone_raw:
        return False
    parsed = normalise(result.phone_raw)
    if parsed is None:
        return False

    existing = {c.value_e164 for c in business.contacts if c.value_e164}
    if parsed.e164 in existing:
        return False

    evidence = score_signals([baseline_signal(parsed.line_type)])
    session.add(
        Contact(
            business_id=business.id,
            kind=ContactKind.PHONE,
            value_raw=result.phone_raw,
            value_e164=parsed.e164,
            line_type=parsed.line_type,
            operator=parsed.operator,
            wa_evidence=round(evidence.score, 2),
            wa_label=evidence.label,
            # Maps publishes the business's own listed number. It is not
            # attributed to a person — §8 Tier B may later infer that the listed
            # number *is* the owner's, but that inference belongs to Stage 4 and
            # must never be asserted here.
            belongs_to=BelongsTo.BUSINESS,
            confidence=0.85,
            source=Source.GOOGLE_MAPS,
            source_url=result.maps_url or "https://www.google.com/maps",
            scraped_at=datetime.now(UTC),
        )
    )
    session.flush()
    return True

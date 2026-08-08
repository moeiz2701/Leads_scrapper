"""§10.1 ingest and cross-query dedupe, plus §10.1's text normalisation."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from leadscraper.core.maps_payload import MapsResult
from leadscraper.core.textnorm import classify_site_url, normalise_name, registrable_domain
from leadscraper.db.models import Business, Contact, Run
from leadscraper.enums import (
    BelongsTo,
    LineType,
    NumberPreference,
    RunStatus,
    Source,
    WhatsAppLabel,
)
from leadscraper.services.ingest import IngestStats, ingest_maps_result
from tests.conftest import requires_db

# --------------------------------------------------------------------------- #
# Text normalisation — pure, no DB
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Paragon Salon", "paragon salon"),
        ("Paragon Salon!", "Paragon  Salon"),
        ("Café Rouge", "Cafe Rouge"),
        ("Al-Fatah Store", "Al Fatah Store"),
        ("Khan Motors Pvt Ltd", "Khan Motors"),
        ("Khan Motors (Pvt) Limited", "Khan Motors"),
    ],
)
def test_names_that_should_normalise_alike(a: str, b: str) -> None:
    assert normalise_name(a) == normalise_name(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Paragon Salon Gulberg", "Paragon Salon DHA"),
        ("Khan Motors", "Khan Brothers"),
        ("Salon 1", "Salon 2"),
    ],
)
def test_distinguishing_words_are_never_stripped(a: str, b: str) -> None:
    """A false merge destroys a lead; a false split only costs a duplicate row.
    The normaliser is biased accordingly."""
    assert normalise_name(a) != normalise_name(b)


def test_normalise_name_handles_empty() -> None:
    assert normalise_name("") == ""


@pytest.mark.parametrize(
    ("url", "domain"),
    [
        ("https://www.example.pk/contact", "example.pk"),
        ("http://example.com.pk", "example.com.pk"),
        ("example.pk", "example.pk"),
        ("https://EXAMPLE.pk:8443/x", "example.pk"),
    ],
)
def test_registrable_domain(url: str, domain: str) -> None:
    assert registrable_domain(url) == domain


def test_social_urls_route_to_their_own_columns() -> None:
    """§5.1 found 85% of Maps results carry a 'website', and for PK SMBs it is
    frequently an Instagram profile. That is a §6.4 bio-link lead, not a §5.2
    website-module lead, so it must not land in the website column."""
    assert classify_site_url("https://www.instagram.com/paragonsalonlhr/") == (
        None, None, "https://www.instagram.com/paragonsalonlhr/",
    )
    assert classify_site_url("https://facebook.com/somepage") == (
        None, "https://facebook.com/somepage", None,
    )
    assert classify_site_url("https://canvassalon.com.pk/") == (
        "https://canvassalon.com.pk/", None, None,
    )
    assert classify_site_url(None) == (None, None, None)


# --------------------------------------------------------------------------- #
# Ingest — needs the DB
# --------------------------------------------------------------------------- #

def _run(session: Session) -> Run:
    run = Run(
        city="Lahore",
        category="salon",
        number_pref=NumberPreference.OWNER_FIRST,
        sources_enabled={"google_maps": True},
        status=RunStatus.RUNNING,
    )
    session.add(run)
    session.flush()
    return run


def _result(**overrides) -> MapsResult:
    base = {
        "name": "Paragon Salon Gulberg",
        "place_id": "ChIJt107G8EFGTkRnh2as4GxD3Y",
        "address": "59-B-3, MM Alam Road, Lahore",
        "lat": 31.5065,
        "lng": 74.3500,
        "phone_raw": "+92 42 32294007",
        "website": "https://www.instagram.com/paragonsalonlhr/",
        "rating": 4.7,
        "review_count": None,
        "price_range": None,
        "category": "Hair salon",
    }
    return MapsResult(**{**base, **overrides})


@requires_db
def test_ingest_creates_business_and_contact(db_session: Session) -> None:
    run = _run(db_session)
    stats = IngestStats()
    business = ingest_maps_result(
        db_session, run.id, _result(), area="Gulberg", city="Lahore",
        category="salon", stats=stats,
    )
    assert business is not None
    assert stats.created == 1 and stats.contacts_added == 1
    assert business.area == "Gulberg"
    assert business.instagram_url is not None
    assert business.website is None

    contact = db_session.execute(select(Contact)).scalar_one()
    assert contact.value_e164 == "+924232294007"
    assert contact.line_type == LineType.LANDLINE
    assert contact.source == Source.GOOGLE_MAPS


@requires_db
def test_maps_contact_is_attributed_to_the_business_not_a_person(
    db_session: Session,
) -> None:
    """§8: Maps publishes the business's listed number. Tier B may later infer
    it is the owner's, but that inference is Stage 4's and must never be
    asserted at ingest."""
    run = _run(db_session)
    ingest_maps_result(db_session, run.id, _result())
    contact = db_session.execute(select(Contact)).scalar_one()
    assert contact.belongs_to == BelongsTo.BUSINESS
    assert contact.person_name is None
    assert contact.person_role is None
    assert contact.attribution is None


@requires_db
def test_landline_gets_no_whatsapp_credit(db_session: Session) -> None:
    run = _run(db_session)
    ingest_maps_result(db_session, run.id, _result())
    contact = db_session.execute(select(Contact)).scalar_one()
    assert contact.wa_label == WhatsAppLabel.NO
    assert float(contact.wa_evidence) == 0.0


@requires_db
def test_mobile_gets_the_likely_baseline(db_session: Session) -> None:
    """§9.3: an 03xx mobile with no other signal scores 0.60 / likely."""
    run = _run(db_session)
    ingest_maps_result(db_session, run.id, _result(phone_raw="+92 321 1885256"))
    contact = db_session.execute(select(Contact)).scalar_one()
    assert contact.wa_label == WhatsAppLabel.LIKELY
    assert float(contact.wa_evidence) == 0.60


@requires_db
def test_same_place_across_queries_collapses_to_one_business(
    db_session: Session,
) -> None:
    """§5.1's fan-out deliberately overlaps — 12 tiles × 5 synonyms yields ~2,400
    raw hits for ~700 real businesses. Without this the table is 3× junk."""
    run = _run(db_session)
    stats = IngestStats()
    for _ in range(4):
        ingest_maps_result(db_session, run.id, _result(), stats=stats)

    assert stats.created == 1
    assert stats.merged == 3
    assert len(db_session.execute(select(Business)).scalars().all()) == 1
    assert len(db_session.execute(select(Contact)).scalars().all()) == 1


@requires_db
def test_merge_fills_gaps_but_never_blanks_a_populated_field(
    db_session: Session,
) -> None:
    run = _run(db_session)
    ingest_maps_result(db_session, run.id, _result(address=None, rating=None))
    ingest_maps_result(db_session, run.id, _result())
    business = db_session.execute(select(Business)).scalar_one()
    assert business.address == "59-B-3, MM Alam Road, Lahore"
    assert float(business.rating) == 4.7

    # A later, thinner payload must not undo it.
    ingest_maps_result(db_session, run.id, _result(address=None, rating=None))
    db_session.flush()
    assert business.address == "59-B-3, MM Alam Road, Lahore"


@requires_db
def test_review_count_arrives_from_whichever_payload_has_it(
    db_session: Session,
) -> None:
    """§5.1: payload richness varies between responses, so the count may only
    appear on a later hit for the same place."""
    run = _run(db_session)
    ingest_maps_result(db_session, run.id, _result(review_count=None))
    ingest_maps_result(db_session, run.id, _result(review_count=1435))
    business = db_session.execute(select(Business)).scalar_one()
    assert business.review_count == 1435


@requires_db
def test_a_second_number_is_added_not_replaced(db_session: Session) -> None:
    """§10.1: 'Never discard a contact during merge — a second number is a
    second column, and that's exactly what you asked for.'"""
    run = _run(db_session)
    stats = IngestStats()
    ingest_maps_result(db_session, run.id, _result(), stats=stats)
    ingest_maps_result(db_session, run.id, _result(phone_raw="0321-1885256"), stats=stats)

    contacts = db_session.execute(select(Contact)).scalars().all()
    assert {c.value_e164 for c in contacts} == {"+924232294007", "+923211885256"}
    assert stats.contacts_added == 2


@requires_db
def test_unparseable_phone_does_not_create_a_contact(db_session: Session) -> None:
    run = _run(db_session)
    stats = IngestStats()
    ingest_maps_result(db_session, run.id, _result(phone_raw="call us!"), stats=stats)
    assert stats.created == 1
    assert db_session.execute(select(Contact)).scalars().all() == []


@requires_db
def test_result_without_identity_is_dropped(db_session: Session) -> None:
    run = _run(db_session)
    assert ingest_maps_result(db_session, run.id, _result(name=None, place_id=None)) is None


@requires_db
def test_the_same_place_in_two_runs_stays_separate(db_session: Session) -> None:
    run_a, run_b = _run(db_session), _run(db_session)
    ingest_maps_result(db_session, run_a.id, _result())
    ingest_maps_result(db_session, run_b.id, _result())
    assert len(db_session.execute(select(Business)).scalars().all()) == 2

"""DB-backed behaviour: §11 schema constraints and §7 cache TTL.

Skipped when Postgres is not running.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leadscraper.core.cache import FetchCache, FetchKind, url_hash
from leadscraper.db.models import Business, Contact, DoNotContact, RawFetch, Run
from leadscraper.enums import (
    Attribution,
    ContactKind,
    LineType,
    NumberPreference,
    PersonRole,
    RunStatus,
    Source,
    WhatsAppLabel,
)
from tests.conftest import requires_db

pytestmark = requires_db


def _make_run(session: Session, **overrides) -> Run:
    run = Run(
        city="Lahore",
        category="salon",
        number_pref=NumberPreference.OWNER_FIRST,
        sources_enabled={"google_maps": True, "business_website": True},
        status=RunStatus.QUEUED,
        **overrides,
    )
    session.add(run)
    session.flush()
    return run


def test_full_row_round_trip(db_session: Session) -> None:
    run = _make_run(db_session)
    business = Business(
        run_id=run.id,
        name="Allure Beauty Salon",
        name_norm="allure beauty salon",
        category="salon",
        city="Lahore",
        area="Gulberg",
        lat="31.516100",
        lng="74.343300",
        place_id="ChIJexample",
        rating="4.5",
        review_count=212,
    )
    db_session.add(business)
    db_session.flush()

    db_session.add(
        Contact(
            business_id=business.id,
            kind=ContactKind.PHONE,
            value_raw="0300-1234567",
            value_e164="+923001234567",
            line_type=LineType.MOBILE,
            operator="Jazz / Mobilink",
            wa_evidence="1.00",
            wa_label=WhatsAppLabel.CONFIRMED,
            person_name="Ayesha Khan",
            person_role=PersonRole.OWNER,
            attribution=Attribution.LINKED,
            belongs_to="owner",
            confidence="0.90",
            source=Source.BUSINESS_WEBSITE,
            source_url="https://allure.pk/contact",
            rank=1,
            scraped_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    loaded = db_session.execute(
        select(Business).where(Business.id == business.id)
    ).scalar_one()
    assert len(loaded.contacts) == 1
    assert loaded.contacts[0].wa_label == WhatsAppLabel.CONFIRMED
    assert loaded.contacts[0].belongs_to == "owner"


def test_place_id_is_unique_within_a_run(db_session: Session) -> None:
    run = _make_run(db_session)
    for _ in range(2):
        db_session.add(
            Business(run_id=run.id, name="X", name_norm="x", place_id="ChIJsame")
        )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_the_same_place_can_appear_in_two_runs(db_session: Session) -> None:
    """§11 declares place_id globally UNIQUE, which would make re-running
    Lahore × salon impossible — yet §16 asks you to re-run for validation.
    Uniqueness is scoped to the run instead."""
    run_a = _make_run(db_session)
    run_b = _make_run(db_session)
    db_session.add(Business(run_id=run_a.id, name="X", name_norm="x", place_id="ChIJsame"))
    db_session.add(Business(run_id=run_b.id, name="X", name_norm="x", place_id="ChIJsame"))
    db_session.flush()  # must not raise


def test_deleting_a_run_cascades(db_session: Session) -> None:
    run = _make_run(db_session)
    business = Business(run_id=run.id, name="X", name_norm="x")
    db_session.add(business)
    db_session.flush()
    db_session.add(
        Contact(
            business_id=business.id,
            kind=ContactKind.PHONE,
            value_raw="0300-1234567",
            source=Source.GOOGLE_MAPS,
            source_url="https://maps.google.com/x",
        )
    )
    db_session.flush()

    db_session.delete(run)
    db_session.flush()
    assert db_session.execute(select(Contact)).scalars().all() == []
    assert db_session.execute(select(Business)).scalars().all() == []


def test_do_not_contact_survives_runs(db_session: Session) -> None:
    """§15: a removal request must not be undone by the next scrape. The table
    has no run_id, which is the structural guarantee of that."""
    db_session.add(DoNotContact(value_e164="+923001234567", reason="requested removal"))
    db_session.flush()
    entry = db_session.execute(select(DoNotContact)).scalar_one()
    assert not hasattr(entry, "run_id")
    assert entry.value_e164 == "+923001234567"


# --------------------------------------------------------------------------- #
# §7 cache
# --------------------------------------------------------------------------- #


def test_cache_put_get_round_trip(fetch_cache: FetchCache) -> None:
    url = "https://businesslist.pk/company/12345"
    body = b"<html><a href='https://wa.me/923001234567'>chat</a></html>"
    fetch_cache.put(url, body, content_type="text/html", source=Source.BUSINESSLIST_PK)

    cached = fetch_cache.get(url)
    assert cached is not None
    assert cached.body == body
    assert "wa.me" in cached.text()


def test_cache_miss_returns_none(fetch_cache: FetchCache) -> None:
    assert fetch_cache.get("https://example.com/never-fetched") is None


def test_cache_normalises_urls_on_the_way_in_and_out(fetch_cache: FetchCache) -> None:
    fetch_cache.put("https://www.example.com/contact/", b"body")
    assert fetch_cache.get("https://example.com/contact?utm_source=x") is not None


@pytest.mark.parametrize(
    ("kind", "expected_days"),
    [(FetchKind.LISTING, 7), (FetchKind.DETAIL, 30)],
)
def test_ttl_by_fetch_kind(fetch_cache: FetchCache, kind: FetchKind, expected_days: int) -> None:
    """§2: 7 days for listings, 30 for detail pages."""
    assert fetch_cache.ttl_for(kind) == timedelta(days=expected_days)


def test_expired_entry_is_a_miss_but_the_body_survives(
    fetch_cache: FetchCache, db_session: Session
) -> None:
    """Expiry controls re-fetching, not retention. §2 wants every raw body kept
    so a selector break is a re-parse, not a re-scrape."""
    url = "https://example.com/listing"
    fetch_cache.put(url, b"body", kind=FetchKind.LISTING)

    row = db_session.get(RawFetch, url_hash(url))
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    assert fetch_cache.get(url) is None
    assert fetch_cache.is_fresh(url, FetchKind.LISTING) is False
    replay = fetch_cache.get(url, ignore_ttl=True)
    assert replay is not None and replay.body == b"body"


def test_is_fresh_is_true_immediately_after_put(fetch_cache: FetchCache) -> None:
    url = "https://example.com/detail"
    fetch_cache.put(url, b"body", kind=FetchKind.DETAIL)
    assert fetch_cache.is_fresh(url, FetchKind.DETAIL) is True


def test_put_twice_updates_rather_than_duplicates(
    fetch_cache: FetchCache, db_session: Session
) -> None:
    url = "https://example.com/detail"
    fetch_cache.put(url, b"first")
    fetch_cache.put(url, b"second")
    rows = db_session.execute(select(RawFetch)).scalars().all()
    assert len(rows) == 1
    cached = fetch_cache.get(url)
    assert cached is not None and cached.body == b"second"


def test_cache_stats(fetch_cache: FetchCache) -> None:
    fetch_cache.put("https://example.com/a", b"x" * 10)
    fetch_cache.put("https://example.com/b", b"y" * 20)
    stats = fetch_cache.stats()
    assert stats["entries"] == 2
    assert stats["bytes"] == 30


def test_run_stats_jsonb_round_trip(db_session: Session) -> None:
    run = _make_run(db_session, stats={"discovered": 700, "qualified": 380})
    db_session.flush()
    db_session.expire(run)
    assert run.stats == {"discovered": 700, "qualified": 380}


def test_subcategories_array_round_trip(db_session: Session) -> None:
    run = _make_run(db_session, subcategories=["barber", "spa"])
    db_session.flush()
    db_session.expire(run)
    assert run.subcategories == ["barber", "spa"]


def test_run_id_is_a_uuid(db_session: Session) -> None:
    run = _make_run(db_session)
    assert isinstance(run.id, uuid.UUID)

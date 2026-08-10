"""Stage 2 — folding §5.2 website evidence into the contacts Stage 1 produced.

These run against the real database with a fake HTTP client, because the thing
worth testing is the *merge*: what happens when a source that can prove WhatsApp
meets a contact row that could only guess at it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from leadscraper.core.pacing import PacingPolicy
from leadscraper.db.models import Business, Contact, Run
from leadscraper.enums import (
    Attribution,
    BelongsTo,
    ContactKind,
    LineType,
    NumberPreference,
    PersonRole,
    RunStatus,
    Source,
    WhatsAppLabel,
)
from leadscraper.services.enrichment import YIELD_FLOOR_SITES, enrich_run
from leadscraper.sources.website import WebsiteSource
from tests.conftest import requires_db
from tests.test_website_source import FakeClient, _html, _settings

HOME = "https://salonx.pk/"
MAPS_URL = "https://www.google.com/maps/place/?q=place_id:ChIJa"
HOMEPAGE_WITH_WA = '<body><a href="https://wa.me/923001234567">Chat</a></body>'


def _run(session: Session) -> Run:
    run = Run(
        city="Lahore",
        category="salon",
        number_pref=NumberPreference.OWNER_FIRST,
        sources_enabled={"google_maps": True, "business_website": True},
        status=RunStatus.DONE,
    )
    session.add(run)
    session.flush()
    return run


def _business(session: Session, run: Run, website: str | None = HOME, **overrides) -> Business:
    business = Business(
        run_id=run.id,
        name=overrides.pop("name", "Salon X"),
        name_norm=overrides.pop("name_norm", "salon x"),
        city="Lahore",
        website=website,
        **overrides,
    )
    session.add(business)
    session.flush()
    return business


def _maps_contact(session: Session, business: Business, e164: str = "+923001234567") -> Contact:
    """A contact exactly as Stage 1 leaves it: a mobile, scored `likely`."""
    contact = Contact(
        business_id=business.id,
        kind=ContactKind.PHONE,
        value_raw="0300-1234567",
        value_e164=e164,
        line_type=LineType.MOBILE,
        operator="Jazz / Mobilink",
        wa_evidence=0.60,
        wa_label=WhatsAppLabel.LIKELY,
        belongs_to=BelongsTo.BUSINESS,
        confidence=0.85,
        source=Source.GOOGLE_MAPS,
        source_url=MAPS_URL,
    )
    session.add(contact)
    session.flush()
    return contact


def _source(pages: dict) -> WebsiteSource:
    client = FakeClient(pages)
    return WebsiteSource(
        client=client,
        settings=_settings(),
        policy=PacingPolicy(delay_min=0.0, delay_max=0.0, concurrency=3),
    )


def _phones(session: Session, business: Business) -> list[Contact]:
    return list(
        session.execute(
            select(Contact).where(
                Contact.business_id == business.id, Contact.kind == ContactKind.PHONE
            )
        ).scalars()
    )


# --------------------------------------------------------------------------- #
# The point of Phase 3
# --------------------------------------------------------------------------- #


@requires_db
def test_a_wa_link_upgrades_a_maps_number_from_likely_to_confirmed(
    db_session: Session,
) -> None:
    """Before this stage nothing in the pipeline can produce a `confirmed`
    label — §9.3 scores a bare 03xx at 0.60 and that is all Maps gives you."""
    run = _run(db_session)
    business = _business(db_session, run)
    contact = _maps_contact(db_session, business)

    source = _source({HOME: _html('<body><a href="https://wa.me/923001234567">Chat</a></body>')})
    report = enrich_run(db_session, run, source=source)

    db_session.refresh(contact)
    assert contact.wa_label == WhatsAppLabel.CONFIRMED
    assert float(contact.wa_evidence) == 1.00
    assert report.contacts_upgraded == 1 and report.confirmed_whatsapp == 1


@requires_db
def test_the_upgrade_records_where_the_proof_came_from(db_session: Session) -> None:
    """§1: every record says which URL it came from. The number came from Maps
    and the proof came from the website, so `source_url` and `wa_evidence_url`
    have to be able to disagree."""
    run = _run(db_session)
    business = _business(db_session, run)
    contact = _maps_contact(db_session, business)

    source = _source(
        {
            HOME: _html('<body><p>0300-1234567</p><a href="/contact/">Contact</a></body>'),
            "https://salonx.pk/contact/": _html(
                '<body><a href="https://wa.me/923001234567">Chat</a></body>'
            ),
        }
    )
    enrich_run(db_session, run, source=source)

    db_session.refresh(contact)
    assert contact.wa_evidence_url == "https://salonx.pk/contact/"
    assert contact.source == Source.GOOGLE_MAPS
    assert contact.source_url == MAPS_URL


@requires_db
def test_a_number_only_the_site_publishes_becomes_a_new_contact(
    db_session: Session,
) -> None:
    """§10.1: "Never discard a contact... a second number is a second column."""
    run = _run(db_session)
    business = _business(db_session, run)
    _maps_contact(db_session, business)

    source = _source(
        {
            HOME: _html(
                """<body><a href="https://wa.me/923219876543">Bridal WhatsApp</a>
                   <p>0300-1234567</p></body>"""
            )
        }
    )
    report = enrich_run(db_session, run, source=source)

    by_number = {c.value_e164: c for c in _phones(db_session, business)}
    assert set(by_number) == {"+923001234567", "+923219876543"}
    new = by_number["+923219876543"]
    assert new.source == Source.BUSINESS_WEBSITE
    assert new.source_url == HOME
    assert new.wa_label == WhatsAppLabel.CONFIRMED
    assert report.contacts_added == 1


@requires_db
def test_website_evidence_never_downgrades_a_stronger_earlier_score(
    db_session: Session,
) -> None:
    """A site that happens not to mention WhatsApp is not evidence *against* a
    number. Absence of evidence must not overwrite evidence."""
    run = _run(db_session)
    business = _business(db_session, run)
    contact = _maps_contact(db_session, business)
    contact.wa_evidence = 1.00
    contact.wa_label = WhatsAppLabel.CONFIRMED
    contact.wa_evidence_url = "https://elsewhere.pk/"
    db_session.flush()

    source = _source({HOME: _html('<body><a href="tel:03001234567">Call</a></body>')})
    enrich_run(db_session, run, source=source)

    db_session.refresh(contact)
    assert float(contact.wa_evidence) == 1.00
    assert contact.wa_evidence_url == "https://elsewhere.pk/"


@requires_db
def test_running_the_stage_twice_adds_nothing_the_second_time(
    db_session: Session,
) -> None:
    run = _run(db_session)
    business = _business(db_session, run)
    _maps_contact(db_session, business)
    pages = {
        HOME: _html(
            """<body><a href="https://wa.me/923001234567">Chat</a>
               <a href="mailto:info@salonx.pk">Email</a></body>"""
        )
    }

    enrich_run(db_session, run, source=_source(pages))
    before = len(_phones(db_session, business))
    second = enrich_run(db_session, run, source=_source(pages))

    assert len(_phones(db_session, business)) == before
    assert second.contacts_added == 0 and second.emails_added == 0


# --------------------------------------------------------------------------- #
# §8 attribution
# --------------------------------------------------------------------------- #


@requires_db
def test_a_jsonld_founder_is_linked_to_the_number_in_the_same_record(
    db_session: Session,
) -> None:
    run = _run(db_session)
    business = _business(db_session, run)

    source = _source(
        {
            HOME: _html(
                """<body><script type="application/ld+json">
                  {"@type":"BeautySalon","name":"Salon X","telephone":"0300-1234567",
                   "founder":{"@type":"Person","name":"Ayesha Khan"}}
                </script></body>"""
            )
        }
    )
    report = enrich_run(db_session, run, source=source)

    contact = _phones(db_session, business)[0]
    assert contact.person_name == "Ayesha Khan"
    assert contact.person_role == PersonRole.OWNER
    assert contact.attribution == Attribution.LINKED
    assert contact.belongs_to == BelongsTo.OWNER
    assert report.people_linked == 1


@requires_db
def test_an_existing_person_name_is_not_overwritten(db_session: Session) -> None:
    """Stage 4 owns attribution. This stage gap-fills and gets out of the way."""
    run = _run(db_session)
    business = _business(db_session, run)
    contact = _maps_contact(db_session, business)
    contact.person_name = "Someone Else"
    contact.person_role = PersonRole.DIRECTOR
    db_session.flush()

    source = _source(
        {
            HOME: _html(
                """<body><script type="application/ld+json">
                  {"@type":"BeautySalon","name":"X","telephone":"0300-1234567",
                   "founder":{"@type":"Person","name":"Ayesha Khan"}}
                </script></body>"""
            )
        }
    )
    enrich_run(db_session, run, source=source)

    db_session.refresh(contact)
    assert contact.person_name == "Someone Else"


# --------------------------------------------------------------------------- #
# Emails and socials
# --------------------------------------------------------------------------- #


@requires_db
def test_emails_land_as_contacts_and_are_capped(db_session: Session) -> None:
    run = _run(db_session)
    business = _business(db_session, run)
    links = "".join(f'<a href="mailto:dept{i}@salonx.pk">m</a>' for i in range(6))

    enrich_run(db_session, run, source=_source({HOME: _html(f"<body>{links}</body>")}))

    emails = list(
        db_session.execute(
            select(Contact).where(
                Contact.business_id == business.id, Contact.kind == ContactKind.EMAIL
            )
        ).scalars()
    )
    assert len(emails) == 3
    assert all(e.source == Source.BUSINESS_WEBSITE for e in emails)


@requires_db
def test_social_links_gap_fill_the_stage_3_columns(db_session: Session) -> None:
    run = _run(db_session)
    business = _business(db_session, run, instagram_url="https://instagram.com/known")

    source = _source(
        {
            HOME: _html(
                """<body><a href="https://facebook.com/salonx">fb</a>
                   <a href="https://instagram.com/other">ig</a></body>"""
            )
        }
    )
    enrich_run(db_session, run, source=source)

    db_session.refresh(business)
    assert business.facebook_url == "https://facebook.com/salonx"
    assert business.instagram_url == "https://instagram.com/known"


# --------------------------------------------------------------------------- #
# Selection and bookkeeping
# --------------------------------------------------------------------------- #


@requires_db
def test_businesses_without_a_website_are_skipped_not_failed(db_session: Session) -> None:
    run = _run(db_session)
    _business(db_session, run, website=None, name="No Site")
    _business(db_session, run, website=HOME, name="Has Site")

    report = enrich_run(db_session, run, source=_source({HOME: _html("<body>x</body>")}))
    assert report.businesses_total == 2
    assert report.with_website == 1
    assert report.domains_crawled == 1


@requires_db
def test_a_shared_domain_is_crawled_once_and_credited_to_both(
    db_session: Session,
) -> None:
    """Branches of a chain share a site. Crawling it twice is duplicated
    politeness cost for a result the cache would have served anyway."""
    run = _run(db_session)
    a = _business(db_session, run, name="Salon X DHA")
    b = _business(db_session, run, name="Salon X Gulberg")

    source = _source({HOME: _html('<body><a href="https://wa.me/923001234567">W</a></body>')})
    report = enrich_run(db_session, run, source=source)

    assert report.domains_crawled == 1
    assert source._client.requested == [HOME]
    assert len(_phones(db_session, a)) == 1
    assert len(_phones(db_session, b)) == 1


@requires_db
def test_a_run_with_no_websites_at_all_does_not_crawl(db_session: Session) -> None:
    run = _run(db_session)
    _business(db_session, run, website=None)
    source = _source({})
    report = enrich_run(db_session, run, source=source)
    assert report.domains_crawled == 0 and source._client.requested == []


@requires_db
def test_the_stage_records_its_counters_on_the_run(db_session: Session) -> None:
    from leadscraper.services.enrichment import run_website_enrichment  # noqa: F401

    run = _run(db_session)
    _business(db_session, run)
    report = enrich_run(
        db_session,
        run,
        source=_source({HOME: _html('<body><a href="tel:03001234567">Call</a></body>')}),
    )
    assert report.as_dict()["phones_found"] == 1
    assert report.pages_fetched == 1


# --------------------------------------------------------------------------- #
# §5.5 — never let a source silently return zero
# --------------------------------------------------------------------------- #


@requires_db
def test_a_stage_that_parses_many_live_sites_and_finds_nothing_says_so(
    db_session: Session,
) -> None:
    """The §5.5 failure mode at the stage level. Twenty live sites yielding no
    number at all means the extractors stopped matching, and a run that reports
    `done` on that is how you ship 1,500 blank rows."""
    run = _run(db_session)
    pages = {}
    for i in range(YIELD_FLOOR_SITES):
        url = f"https://salon{i}.pk/"
        _business(db_session, run, website=url, name=f"Salon {i}")
        # Live, parseable, and carrying an email but no phone — so the domains
        # count as productive while the phone extractors produce nothing.
        pages[url] = _html(f'<body><a href="mailto:a@salon{i}.pk">Mail</a></body>')

    report = enrich_run(db_session, run, source=_source(pages))

    assert report.sites_ok == YIELD_FLOOR_SITES
    assert report.phones_found == 0
    assert run.status == RunStatus.PARTIAL
    assert "no phone numbers" in (run.error or "")


@requires_db
def test_a_normal_run_stays_done(db_session: Session) -> None:
    run = _run(db_session)
    _business(db_session, run)
    enrich_run(
        db_session,
        run,
        source=_source({HOME: _html('<body><a href="tel:03001234567">Call</a></body>')}),
    )
    assert run.status == RunStatus.DONE


@requires_db
def test_one_host_behind_a_waf_does_not_make_the_whole_run_partial(
    db_session: Session,
) -> None:
    """A refusing host is a per-record outcome — some SMB sites sit behind a
    WAF, and a run of 200 domains where 3 say no is a healthy run, not a
    degraded one. What degrades a run is work it planned and skipped."""
    run = _run(db_session)
    _business(db_session, run)
    report = enrich_run(db_session, run, source=_source({HOME: (403, b"", "text/html")}))

    assert report.sites_refused == 1 and report.sites_blocked == 0
    assert run.status == RunStatus.DONE


@requires_db
def test_domains_the_stage_never_reached_do_make_the_run_partial(
    db_session: Session,
) -> None:
    run = _run(db_session)
    _business(db_session, run)
    source = _source({HOME: _html(HOMEPAGE_WITH_WA)})
    source.breaker.record_blocked(429)  # the module already stopped

    report = enrich_run(db_session, run, source=source)
    assert report.sites_blocked == 1
    assert run.status == RunStatus.PARTIAL
    assert "never crawled" in (run.error or "")


@requires_db
def test_a_stage_1_failure_is_not_upgraded_by_stage_2(db_session: Session) -> None:
    """§13 Screen 2 shows run status. A discovery run that half-failed stays
    failed however well the websites went."""
    run = _run(db_session)
    run.status = RunStatus.FAILED
    run.error = "every query was blocked or failed"
    _business(db_session, run)

    enrich_run(
        db_session,
        run,
        source=_source({HOME: _html('<body><a href="tel:03001234567">Call</a></body>')}),
    )
    assert run.status == RunStatus.FAILED

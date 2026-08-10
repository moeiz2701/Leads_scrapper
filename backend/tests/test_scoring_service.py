"""Stage 5 against a real database — §10.2 scoring and §3.3 ranking."""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from leadscraper.db.models import Business, Contact, Run
from leadscraper.enums import (
    BelongsTo,
    ContactKind,
    LineType,
    NumberPreference,
    RunStatus,
    Source,
    WhatsAppLabel,
)
from leadscraper.services.scoring import score_run
from tests.conftest import requires_db


def _run(session: Session, preference=NumberPreference.OWNER_FIRST) -> Run:
    run = Run(
        city="Islamabad",
        category="salon",
        number_pref=preference,
        sources_enabled={"google_maps": True, "business_website": True},
        status=RunStatus.RUNNING,
    )
    session.add(run)
    session.flush()
    return run


_SEQUENCE = itertools.count(1)


def _number() -> str:
    """A distinct, valid-looking PK mobile per call."""
    return f"+9230012{next(_SEQUENCE):05d}"


def _business(session: Session, run: Run, **overrides) -> Business:
    base = dict(
        name="Paragon Salon",
        name_norm="paragon salon",
        city="Islamabad",
        area="F-7",
        address="Jinnah Super Market, F-7 Markaz",
        lat=33.7167,
        lng=73.0552,
        place_id=f"place-{uuid.uuid4()}",
        rating=4.6,
        review_count=31,
    )
    business = Business(run_id=run.id, **{**base, **overrides})
    session.add(business)
    session.flush()
    return business


def _phone(
    session: Session,
    business: Business,
    e164: str,
    *,
    line_type=LineType.MOBILE,
    wa=0.60,
    label=WhatsAppLabel.LIKELY,
    confidence=0.85,
    source=Source.GOOGLE_MAPS,
    person_name=None,
    belongs_to=BelongsTo.BUSINESS,
) -> Contact:
    contact = Contact(
        business_id=business.id,
        kind=ContactKind.PHONE,
        value_raw=e164,
        value_e164=e164,
        line_type=line_type,
        wa_evidence=wa,
        wa_label=label,
        confidence=confidence,
        person_name=person_name,
        belongs_to=belongs_to,
        source=source,
        source_url="https://www.google.com/maps",
        scraped_at=datetime.now(UTC),
    )
    business.contacts.append(contact)
    session.flush()
    return contact


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


@requires_db
def test_scoring_writes_a_lead_score_and_ranks_the_contacts(db_session: Session) -> None:
    run = _run(db_session)
    business = _business(db_session, run)
    _phone(db_session, business, "+923001234567")
    _phone(db_session, business, "+924232294007", line_type=LineType.LANDLINE,
           wa=0.0, label=WhatsAppLabel.NO)

    report = score_run(db_session, run)

    assert report.scored == 1
    assert business.lead_score is not None and 0 <= business.lead_score <= 100
    by_number = {c.value_e164: c.rank for c in business.contacts}
    assert by_number["+923001234567"] == 1  # mobile outranks landline
    assert by_number["+924232294007"] == 2


@requires_db
def test_a_missing_review_count_does_not_sink_the_lead(db_session: Session) -> None:
    """The Lahore × salon run carries no review counts at all (0 of 60), against
    80% on Islamabad. If missing scored as zero reviews, that whole run would
    rank below Islamabad for a §5.1 payload artefact rather than for anything
    about the businesses."""
    run = _run(db_session)
    known = _business(db_session, run, review_count=31)
    unknown = _business(db_session, run, review_count=None)
    fabricated = _business(db_session, run, review_count=0)
    for business in (known, unknown, fabricated):
        _phone(db_session, business, _number())

    score_run(db_session, run)

    assert unknown.lead_score > fabricated.lead_score
    assert abs(unknown.lead_score - known.lead_score) <= 5


@requires_db
def test_the_run_reports_which_business_signal_inputs_it_had(db_session: Session) -> None:
    run = _run(db_session)
    _business(db_session, run, rating=4.6, review_count=31)
    _business(db_session, run, rating=4.7, review_count=None)
    _business(db_session, run, rating=None, review_count=None)

    report = score_run(db_session, run)

    assert report.business_signal_basis == {
        "rating_and_reviews": 1,
        "rating_only": 1,
        "none": 1,
    }


@requires_db
def test_the_run_reports_the_ceiling_an_unattributed_row_cannot_beat(
    db_session: Session,
) -> None:
    """§8's attribution engine is Phase 9, so the 15-point person term is 0 for
    almost every row: 1 business in 199 on the live Islamabad run. A row without
    a name therefore tops out at 85, and the run reports that rather than letting
    an operator read the cap as a fact about the businesses."""
    run = _run(db_session)
    unattributed = _business(db_session, run)
    _phone(db_session, unattributed, "+923001234567")

    report = score_run(db_session, run)
    assert report.with_person == 0
    assert report.unattributed_ceiling == 85
    assert unattributed.lead_score <= 85


@requires_db
def test_an_attributed_row_can_pass_the_unattributed_ceiling(
    db_session: Session,
) -> None:
    """The ceiling is a property of the missing term, not of the run — one
    attributed business does not raise it for the other 198, and it must not stop
    that business from scoring above it."""
    run = _run(db_session)
    named = _business(db_session, run, website="https://x.pk", rating=5.0,
                      review_count=1000)
    _phone(db_session, named, "+923001234567", person_name="Hina Khan",
           belongs_to=BelongsTo.OWNER, confidence=0.95, wa=1.00,
           label=WhatsAppLabel.CONFIRMED, source=Source.BUSINESS_WEBSITE)
    _phone(db_session, named, "+923339876543", source=Source.GOOGLE_MAPS)

    report = score_run(db_session, run)
    assert report.with_person == 1
    assert report.unattributed_ceiling == 85
    assert named.lead_score > 85


@requires_db
def test_source_agreement_counts_sources_across_the_business(db_session: Session) -> None:
    """§5.2 measured that only 19 of 53 confirmed numbers were ones Maps also
    carried, so per-number agreement is rare; the signal that exists is a
    business carrying contacts from google_maps *and* business_website."""
    run = _run(db_session)
    one = _business(db_session, run)
    _phone(db_session, one, "+923001234567", source=Source.GOOGLE_MAPS)

    two = _business(db_session, run)
    _phone(db_session, two, "+923001234567", source=Source.GOOGLE_MAPS)
    _phone(db_session, two, "+923339876543", source=Source.BUSINESS_WEBSITE)

    report = score_run(db_session, run)
    assert report.source_agreement == 1
    assert two.lead_score > one.lead_score


@requires_db
def test_online_presence_accepts_a_social_profile_instead_of_a_website(
    db_session: Session,
) -> None:
    """68% of discovered businesses have no website (§5.1/§14). completeness
    treats website/Facebook/Instagram as one disjunction so the market's shape
    does not read as a bad lead."""
    run = _run(db_session)
    site = _business(db_session, run, website="https://paragon.pk")
    insta = _business(db_session, run, instagram_url="https://instagram.com/paragon")
    nothing = _business(db_session, run)
    for business in (site, insta, nothing):
        _phone(db_session, business, _number())

    score_run(db_session, run)
    assert site.lead_score == insta.lead_score > nothing.lead_score


@requires_db
def test_a_business_with_no_contacts_scores_low_but_is_still_scored(
    db_session: Session,
) -> None:
    """25 of the 199 Islamabad businesses carry no phone. That is a measured
    absence, not a missing input — you genuinely cannot contact them — so it
    scores 0 on the contact terms rather than being renormalised away."""
    run = _run(db_session)
    business = _business(db_session, run)

    report = score_run(db_session, run)
    assert report.without_contacts == 1
    assert business.lead_score is not None
    assert not report.qualified


@requires_db
def test_qualified_requires_a_mobile_not_just_a_score(db_session: Session) -> None:
    run = _run(db_session)
    landline_only = _business(db_session, run, website="https://x.pk")
    _phone(db_session, landline_only, "+924232294007", line_type=LineType.LANDLINE,
           wa=0.0, label=WhatsAppLabel.NO, confidence=0.95)

    report = score_run(db_session, run)
    assert report.with_mobile == 0
    assert report.qualified == 0


# --------------------------------------------------------------------------- #
# Ranking through the stage
# --------------------------------------------------------------------------- #


@requires_db
def test_whatsapp_only_unranks_rather_than_deletes(db_session: Session) -> None:
    run = _run(db_session, NumberPreference.WHATSAPP_ONLY)
    business = _business(db_session, run)
    _phone(db_session, business, "+923001234567")
    _phone(db_session, business, "+924232294007", line_type=LineType.LANDLINE,
           wa=0.0, label=WhatsAppLabel.NO)

    report = score_run(db_session, run)

    assert len(business.contacts) == 2  # §10.1: never discard a contact
    ranks = {c.value_e164: c.rank for c in business.contacts}
    assert ranks["+923001234567"] == 1
    assert ranks["+924232294007"] is None
    assert report.contacts_unranked == 1


@requires_db
def test_emails_never_take_a_ranked_phone_slot(db_session: Session) -> None:
    run = _run(db_session)
    business = _business(db_session, run)
    _phone(db_session, business, "+923001234567")
    business.contacts.append(
        Contact(
            business_id=business.id,
            kind=ContactKind.EMAIL,
            value_raw="hello@paragon.pk",
            confidence=0.80,
            source=Source.BUSINESS_WEBSITE,
            source_url="https://paragon.pk",
        )
    )
    db_session.flush()

    score_run(db_session, run)
    email = next(c for c in business.contacts if c.kind == ContactKind.EMAIL)
    assert email.rank is None


@requires_db
def test_changing_the_preference_re_ranks_losslessly(db_session: Session) -> None:
    """§3.3 controls ranking, not the data. Ranking under whatsapp_only then
    switching back must restore every slot."""
    run = _run(db_session)
    business = _business(db_session, run)
    _phone(db_session, business, "+923001234567")
    _phone(db_session, business, "+924232294007", line_type=LineType.LANDLINE,
           wa=0.0, label=WhatsAppLabel.NO)

    score_run(db_session, run)
    before = {c.value_e164: c.rank for c in business.contacts}

    run.number_pref = NumberPreference.WHATSAPP_ONLY
    score_run(db_session, run)
    assert {c.value_e164: c.rank for c in business.contacts}["+924232294007"] is None

    run.number_pref = NumberPreference.OWNER_FIRST
    score_run(db_session, run)
    assert {c.value_e164: c.rank for c in business.contacts} == before


# --------------------------------------------------------------------------- #
# Normalise, and idempotence
# --------------------------------------------------------------------------- #


@requires_db
def test_the_stage_backfills_an_unparsed_contact(db_session: Session) -> None:
    """Stage 1 and 2 both normalise at write time, so this is a no-op today. It
    exists for §5.3's directory modules, which write a raw field-table value."""
    run = _run(db_session)
    business = _business(db_session, run)
    business.contacts.append(
        Contact(
            business_id=business.id,
            kind=ContactKind.PHONE,
            value_raw="0300-1234567",
            source=Source.URDUPOINT,
            source_url="https://www.urdupoint.com/business/x",
        )
    )
    db_session.flush()

    report = score_run(db_session, run)

    contact = business.contacts[0]
    assert contact.value_e164 == "+923001234567"
    assert contact.line_type == LineType.MOBILE
    assert contact.operator == "Jazz / Mobilink"
    assert float(contact.wa_evidence) == 0.60
    assert report.contacts_normalised == 1


@requires_db
def test_normalising_never_lowers_evidence_another_stage_earned(
    db_session: Session,
) -> None:
    """"Evidence only ever moves up." A website's wa.me link proved this number;
    Stage 5 must not reset it to the 0.60 mobile baseline."""
    run = _run(db_session)
    business = _business(db_session, run)
    _phone(db_session, business, "+923001234567", wa=1.00, label=WhatsAppLabel.CONFIRMED,
           source=Source.BUSINESS_WEBSITE)

    score_run(db_session, run)
    contact = business.contacts[0]
    assert float(contact.wa_evidence) == 1.00
    assert contact.wa_label == WhatsAppLabel.CONFIRMED


@requires_db
def test_scoring_is_idempotent(db_session: Session) -> None:
    run = _run(db_session)
    business = _business(db_session, run)
    _phone(db_session, business, "+923001234567")
    _phone(db_session, business, "+923339876543")

    first = score_run(db_session, run)
    score = business.lead_score
    ranks = {c.value_e164: c.rank for c in business.contacts}

    second = score_run(db_session, run)
    assert business.lead_score == score
    assert {c.value_e164: c.rank for c in business.contacts} == ranks
    assert first.as_dict() == second.as_dict()


@requires_db
def test_person_attribution_uses_the_attributed_contacts_confidence(
    db_session: Session,
) -> None:
    run = _run(db_session)
    business = _business(db_session, run)
    _phone(db_session, business, "+923001234567", person_name="Hina Khan",
           confidence=0.70, belongs_to=BelongsTo.OWNER)

    score_run(db_session, run)
    # 15 × 0.70 of the person term must be in the score; the surest check is
    # that removing the name lowers it by exactly that.
    with_name = business.lead_score
    business.contacts[0].person_name = None
    db_session.flush()
    score_run(db_session, run)
    assert with_name - business.lead_score == pytest.approx(
        round(15 * 0.70), abs=1
    )


@requires_db
def test_score_terms_stay_within_their_documented_weights(db_session: Session) -> None:
    run = _run(db_session)
    business = _business(db_session, run, rating=5.0, review_count=6488,
                         website="https://x.pk")
    _phone(db_session, business, "+923001234567", wa=1.00,
           label=WhatsAppLabel.CONFIRMED, confidence=0.95,
           person_name="Hina Khan", belongs_to=BelongsTo.OWNER,
           source=Source.BUSINESS_WEBSITE)
    _phone(db_session, business, "+923339876543", source=Source.GOOGLE_MAPS)
    business.contacts.append(
        Contact(business_id=business.id, kind=ContactKind.EMAIL,
                value_raw="hi@x.pk", confidence=0.80, source=Source.BUSINESS_WEBSITE,
                source_url="https://x.pk")
    )
    db_session.flush()

    score_run(db_session, run)
    assert 90 <= business.lead_score <= 100

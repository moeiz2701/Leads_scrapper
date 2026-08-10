"""Stage 5 orchestration — normalise, score (§10.2), rank (§3.3).

The stage that turns a pile of contacts into a *table*. Everything before this
produces rows; this decides which row an operator should call first and which
business is worth their time at all.

Three things it does, in §2's order — "E.164, WA evidence, rank":

1. **Normalise.** Backfill ``value_e164`` / ``line_type`` / ``operator`` on any
   phone contact whose source wrote a raw value without parsing it. A no-op on
   Maps and website contacts, which normalise at write time; Phase 6's directory
   modules will need it.
2. **Score.** §10.2 onto ``businesses.lead_score``, renormalising over the terms
   whose inputs the source actually published — see ``core/scoring.py``.
3. **Rank.** §3.3 onto ``contacts.rank``, by the run's ``number_pref``.

**Evidence only ever moves up here too.** The normalise pass fills nulls and
never lowers a §9.3 score another stage already earned. Re-running the stage is
idempotent: same inputs, same scores, same ranks.
"""

from __future__ import annotations

import statistics
import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from leadscraper.core.phone import classify, normalise
from leadscraper.core.ranking import rank_contacts
from leadscraper.core.scoring import (
    LeadScore,
    LeadSignals,
    ScoreTerm,
    score_ceiling,
    score_lead,
)
from leadscraper.core.whatsapp import baseline_signal, score_signals
from leadscraper.db.models import Business, Contact, Run
from leadscraper.db.session import session_scope
from leadscraper.enums import ContactKind, LineType, NumberPreference, Stage
from leadscraper.logging import get_logger
from leadscraper.pipeline.stages import StageResult

log = get_logger(__name__)


@dataclass(slots=True)
class ScoringReport:
    businesses_total: int = 0
    scored: int = 0
    # §10.2's two-part definition: score ≥ 60 **and** at least one mobile.
    qualified: int = 0
    with_phone: int = 0
    with_mobile: int = 0
    without_contacts: int = 0

    contacts_ranked: int = 0
    # whatsapp_only excluded them, or a §10.1 merge left two rows on one number.
    contacts_unranked: int = 0
    contacts_normalised: int = 0

    # Which §10.2 terms could actually be evaluated. Reported rather than
    # silently absorbed — see the note on ``score_ceiling`` below.
    business_signal_basis: dict[str, int] = field(default_factory=dict)
    source_agreement: int = 0
    with_person: int = 0
    # The highest score a row with no §8 attribution can reach — 100 minus the
    # 15-point person term. Reported on every run because §8's engine is Phase 9
    # and ``with_person`` is 1 in 199 today: without this an operator reads a
    # table that stops at 85 as a fact about the businesses.
    unattributed_ceiling: int = 100

    score_mean: float = 0.0
    score_p10: int = 0
    score_median: int = 0
    score_p90: int = 0
    score_max: int = 0

    def as_dict(self) -> dict:
        return {
            "businesses_total": self.businesses_total,
            "scored": self.scored,
            "qualified": self.qualified,
            "with_phone": self.with_phone,
            "with_mobile": self.with_mobile,
            "without_contacts": self.without_contacts,
            "contacts_ranked": self.contacts_ranked,
            "contacts_unranked": self.contacts_unranked,
            "contacts_normalised": self.contacts_normalised,
            "business_signal_basis": self.business_signal_basis,
            "source_agreement": self.source_agreement,
            "with_person": self.with_person,
            "unattributed_ceiling": self.unattributed_ceiling,
            "score_mean": self.score_mean,
            "score_p10": self.score_p10,
            "score_median": self.score_median,
            "score_p90": self.score_p90,
            "score_max": self.score_max,
        }


def run_normalise_score(run_id: uuid.UUID) -> StageResult:
    """Score and rank every business in a run."""
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"No such run: {run_id}")

        report = score_run(session, run)
        run.stats = {**(run.stats or {}), "normalise_score": report.as_dict()}
        session.flush()

    return StageResult(
        stage=Stage.NORMALISE_SCORE,
        run_id=run_id,
        processed=report.businesses_total,
        produced=report.qualified,
        skipped=report.without_contacts,
        notes={k: str(v) for k, v in report.as_dict().items()},
    )


def score_run(
    session: Session, run: Run, businesses: list[Business] | None = None
) -> ScoringReport:
    """The stage body, against a caller-supplied session.

    ``businesses`` narrows the pass to specific rows, which is how Stage 6
    re-scores the survivors of a §10.1 merge without re-walking the run.
    """
    report = ScoringReport()
    preference = _preference(run)

    if businesses is None:
        businesses = list(
            session.execute(
                select(Business)
                .where(Business.run_id == run.id)
                .options(selectinload(Business.contacts))
                .order_by(Business.created_at)
            ).scalars()
        )

    report.businesses_total = len(businesses)
    scores: list[int] = []
    bases: Counter[str] = Counter()

    for business in businesses:
        phones = [c for c in business.contacts if c.kind == ContactKind.PHONE]
        report.contacts_normalised += sum(_normalise_contact(c) for c in phones)

        result = _score_business(business, phones)
        business.lead_score = result.score
        scores.append(result.score)
        # `.value`, not the enum member: this dict lands in `runs.stats` (JSONB)
        # and is printed in the run summary, and an enum key renders as its repr
        # in both places.
        bases[result.business_signal_basis.value] += 1

        report.scored += 1
        if result.is_qualified:
            report.qualified += 1
        if phones:
            report.with_phone += 1
        if result.has_mobile:
            report.with_mobile += 1
        if not business.contacts:
            report.without_contacts += 1
        if result.terms.get(ScoreTerm.SOURCE_AGREEMENT):
            report.source_agreement += 1
        if result.terms.get(ScoreTerm.PERSON_ATTRIBUTION):
            report.with_person += 1

        for contact, rank in rank_contacts(phones, preference):
            contact.rank = rank
            if rank is None:
                report.contacts_unranked += 1
            else:
                report.contacts_ranked += 1

        # §12.1 has one `email` column and no ranked email slots, so an email's
        # rank would be meaningless. Cleared rather than left stale, so a re-run
        # after a schema or source change cannot leave a number's slot on one.
        for contact in business.contacts:
            if contact.kind == ContactKind.EMAIL:
                contact.rank = None

    report.business_signal_basis = dict(bases)
    _summarise(report, scores)
    session.flush()
    _finalise(run, report)
    return report


def _preference(run: Run) -> NumberPreference:
    try:
        return NumberPreference(run.number_pref)
    except ValueError:
        # A run row written before a vocabulary change should not take the stage
        # down; §3.3's default is the documented one.
        log.warning("scoring.unknown_preference", run_id=str(run.id), value=run.number_pref)
        return NumberPreference.OWNER_FIRST


# --------------------------------------------------------------------------- #
# Normalise (§9.1, §9.2)
# --------------------------------------------------------------------------- #


def _normalise_contact(contact: Contact) -> bool:
    """Backfill E.164 and classification. Fills nulls only, never corrects.

    A source that already parsed the number is the better authority — it had the
    surrounding page. This exists for sources that write ``value_raw`` and leave
    the rest to the pipeline, which §2's stage split explicitly permits.
    """
    changed = False

    if contact.value_e164 is None and contact.value_raw:
        parsed = normalise(contact.value_raw)
        if parsed is not None:
            contact.value_e164 = parsed.e164
            contact.line_type = contact.line_type or parsed.line_type
            contact.operator = contact.operator or parsed.operator
            changed = True

    if contact.line_type is None and contact.value_e164:
        # E.164 for PK is +92 followed by the national significant number.
        line_type, operator = classify(contact.value_e164.removeprefix("+92"))
        contact.line_type = line_type
        contact.operator = contact.operator or operator
        changed = True

    if contact.wa_evidence is None and contact.line_type:
        # The §9.3 floor, and only ever a floor: a contact that already carries a
        # score keeps it. Website evidence is not undone by a later stage.
        evidence = score_signals([baseline_signal(LineType(contact.line_type))])
        contact.wa_evidence = round(evidence.score, 2)
        contact.wa_label = contact.wa_label or evidence.label
        changed = True

    return changed


# --------------------------------------------------------------------------- #
# Score (§10.2)
# --------------------------------------------------------------------------- #


def _score_business(business: Business, phones: list[Contact]) -> LeadScore:
    return score_lead(_signals(business, phones))


def _signals(business: Business, phones: list[Contact]) -> LeadSignals:
    """Reduce a business and its contacts to §10.2's inputs.

    ``rating``/``review_count`` are passed through as ``None`` when absent. That
    is the whole point — see ``core/scoring``.
    """
    emails = [c for c in business.contacts if c.kind == ContactKind.EMAIL]
    distinct_numbers = {c.value_e164 for c in phones if c.value_e164}

    return LeadSignals(
        whatsapp_evidence=max((float(c.wa_evidence or 0.0) for c in phones), default=0.0),
        # Phones only. An email carries a confidence too, but §10.2's companion
        # qualification test is "at least one mobile" — letting an email supply
        # 25 points of contact confidence to a business with no phone would
        # qualify a lead nobody can ring.
        contact_confidence=max((float(c.confidence or 0.0) for c in phones), default=0.0),
        person_attribution=max(
            (float(c.confidence or 0.0) for c in phones if c.person_name), default=0.0
        ),
        n_sources=len({c.source for c in business.contacts}),
        rating=float(business.rating) if business.rating is not None else None,
        review_count=business.review_count,
        has_address=bool(business.address),
        has_online_presence=bool(
            business.website or business.facebook_url or business.instagram_url
        ),
        has_email=bool(emails),
        has_second_phone=len(distinct_numbers) >= 2,
        has_mobile=any(c.line_type == LineType.MOBILE for c in phones),
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def _summarise(report: ScoringReport, scores: list[int]) -> None:
    report.unattributed_ceiling = score_ceiling((ScoreTerm.PERSON_ATTRIBUTION,))

    if not scores:
        return
    ordered = sorted(scores)
    report.score_mean = round(statistics.fmean(ordered), 1)
    report.score_p10 = ordered[int(len(ordered) * 0.10)]
    report.score_median = ordered[len(ordered) // 2]
    report.score_p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.90))]
    report.score_max = ordered[-1]


def _finalise(run: Run, report: ScoringReport) -> None:
    """§5.5, at the stage level: a scorer that scored nothing must say so."""
    if report.businesses_total and report.scored == 0:
        log.error("scoring.zero_yield", run_id=str(run.id), total=report.businesses_total)
        return
    if report.businesses_total and report.contacts_ranked == 0:
        # Every business has a score but not one number got an export slot. Under
        # owner_first/business_first that is impossible unless the run has no
        # phones at all; under whatsapp_only it means the filter emptied the
        # table, which the operator needs told rather than shown as a blank grid.
        log.warning(
            "scoring.no_ranked_contacts",
            run_id=str(run.id),
            preference=run.number_pref,
            businesses=report.businesses_total,
        )

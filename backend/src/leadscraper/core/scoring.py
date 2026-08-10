"""§10.2 lead score — 0–100, pure, no database.

    score =  30 × whatsapp_evidence          (0–1)
           + 25 × contact_confidence         (0–1)
           + 15 × person_attribution         (0–1)
           + 10 × source_agreement           (n_sources ≥ 2 → 1.0)
           + 10 × business_signal            (reviews/rating, normalised)
           + 10 × completeness               (fields populated ratio)

**Missing is not zero.** §5.1 measured that Maps payload richness varies between
responses and warned that "a fabricated 0 would push good leads *down* the
ranking" — and it was right to: ``review_count`` is present for 80% of the
Islamabad run and **0%** of the Lahore one. Scoring that run's ``business_signal``
as 0 would rank every Lahore business below every Islamabad business for a reason
that has nothing to do with the businesses.

So a term whose inputs the source never published is *omitted*, and the score is
renormalised over the weight that actually applied. A business with no review
data is scored out of 90 and rescaled, not scored out of 100 and docked 10.

**The line between "omitted" and "scored zero"** is whether the underlying fact
plausibly exists and we merely failed to observe it:

* ``business_signal`` — every business has some real level of popularity. Maps
  declining to tell us is our gap, not the business's. **Omitted.**
* ``person_attribution`` — most PK SMB salons genuinely have no publicly named
  owner. "No evidence of a named person" is a true statement about the record,
  not a hole in it. **Scored 0.**

**Omission is partial where the inputs are.** ``business_signal`` has two inputs
and they go missing independently, so it carries half its weight when only one is
present. Measured, and the reason this is not a detail: PK salon ratings cluster
at 4.5–5.0 while review counts spread widely, so rating alone normalises to ~0.90
where rating-and-reviews normalises to ~0.78 for the same typical business. At
full weight, a run whose payload omitted review counts would score *higher* than
one that carried them — the §5.1 bias inverted, but still a bias, and still one
that has nothing to do with the businesses. Half weight plus renormalisation
makes the two cases agree to within a point.

That second one matters right now, because §8's attribution engine is Phase 9 and
today's data carries a named person on 1 business in 199. The term is therefore
~0 for nearly every row, which caps the practical score at 85 out of 100. That
cap is *reported* (see ``score_ceiling``) rather than compensated for: inflating
the other five weights to fill the gap would have to be undone the day Phase 9
lands, and every §16 weight tuned against the inflated scale would be wrong.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

# §10.2's "Good quality lead" bar, applied together with "at least one mobile".
QUALIFIED_SCORE = 60

# Maps ratings run 1.0–5.0, so 1.0 is the floor of the scale, not the middle.
RATING_FLOOR = 1.0
RATING_CEILING = 5.0

# Review count at which a business gets full marks on that half of the signal.
# Measured on the 160 Islamabad businesses that carry a count: median 31, p90
# 399, max 6,488. A log curve with the ceiling at 200 puts the median near 0.65
# and saturates in the top decile, which is the spread that discriminates. A
# linear map would put 95% of the run in the bottom 5% of the range.
REVIEW_SATURATION = 200


class ScoreTerm(StrEnum):
    """The six §10.2 terms, in the order the section lists them."""

    WHATSAPP_EVIDENCE = "whatsapp_evidence"
    CONTACT_CONFIDENCE = "contact_confidence"
    PERSON_ATTRIBUTION = "person_attribution"
    SOURCE_AGREEMENT = "source_agreement"
    BUSINESS_SIGNAL = "business_signal"
    COMPLETENESS = "completeness"


WEIGHTS: dict[ScoreTerm, int] = {
    ScoreTerm.WHATSAPP_EVIDENCE: 30,
    ScoreTerm.CONTACT_CONFIDENCE: 25,
    ScoreTerm.PERSON_ATTRIBUTION: 15,
    ScoreTerm.SOURCE_AGREEMENT: 10,
    ScoreTerm.BUSINESS_SIGNAL: 10,
    ScoreTerm.COMPLETENESS: 10,
}

TOTAL_WEIGHT = sum(WEIGHTS.values())


class BusinessSignalBasis(StrEnum):
    """Which inputs ``business_signal`` was actually computed from.

    Recorded because the answer varies by *run*, not by business: the Lahore
    payloads carried no review counts at all, so that entire run is
    ``RATING_ONLY``. §16's weight tuning needs to know that before it concludes
    anything from comparing the two runs' scores.
    """

    RATING_AND_REVIEWS = "rating_and_reviews"
    RATING_ONLY = "rating_only"
    REVIEWS_ONLY = "reviews_only"
    NONE = "none"


# The fields ``completeness`` counts. Chosen against measured fill rates rather
# than by listing every column, because §10.2's "fields populated ratio" is only
# meaningful over fields whose absence says something about the *lead*:
#
#   * ``address`` (99–100% filled) and ``area`` (100%) are near-constant on Maps
#     data. ``area`` is dropped entirely; ``address`` is kept because §5.3
#     directory records and §3.2 seed rows routinely lack one.
#   * **Having a website is deliberately not one of these.** Only 32% of
#     discovered businesses have one (§5.1, §14) — that is the shape of the PK
#     SMB market, not a defect in the lead. It is folded into a disjunction with
#     the social profiles, so a salon reachable only on Instagram scores the same
#     as one with a domain.
#   * A second phone and an email are alternate channels: they are what the
#     operator falls back on when the first number does not answer.
COMPLETENESS_FIELDS = (
    "has_address",
    "has_online_presence",
    "has_email",
    "has_second_phone",
)


@dataclass(frozen=True, slots=True)
class LeadSignals:
    """One business, reduced to the inputs §10.2 scores.

    ``rating`` and ``review_count`` are ``None`` when the source did not publish
    them. Nothing in this module substitutes a default for either.
    """

    # Max across the business's phone contacts. §9.3's own rule — the strongest
    # signal wins and evidence does not accumulate — applied one level up: a
    # business with one confirmed number is as reachable as one with three.
    whatsapp_evidence: float = 0.0
    contact_confidence: float = 0.0
    person_attribution: float = 0.0

    # §10.2: "n_sources ≥ 2 → 1.0". Counted per business, not per number: after
    # Phase 3 a business routinely carries google_maps *and* business_website
    # contacts, and §5.2 measured that only 19 of 53 confirmed numbers were ones
    # Maps also had — so per-number agreement is rare and per-business is the
    # reading that carries signal.
    n_sources: int = 0

    rating: float | None = None
    review_count: int | None = None

    has_address: bool = False
    has_online_presence: bool = False
    has_email: bool = False
    has_second_phone: bool = False

    # Not a scored term — §10.2's second qualification condition.
    has_mobile: bool = False


@dataclass(frozen=True, slots=True)
class LeadScore:
    score: int
    terms: dict[ScoreTerm, float] = field(default_factory=dict)
    omitted: tuple[ScoreTerm, ...] = ()
    # Fractional, because ``business_signal`` carries half its weight when only
    # one of its two inputs was published.
    applicable_weight: float = float(TOTAL_WEIGHT)
    business_signal_basis: BusinessSignalBasis = BusinessSignalBasis.NONE
    has_mobile: bool = False

    @property
    def is_qualified(self) -> bool:
        """§10.2: score ≥ 60 **and** at least one mobile."""
        return self.score >= QUALIFIED_SCORE and self.has_mobile


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def rating_signal(rating: float) -> float:
    """Normalise a 1–5 star rating onto 0–1.

    Note for §16: this barely discriminates in practice. PK salon ratings cluster
    hard at the top — the Islamabad run's median is 4.6 and its p90 is 5.0, so
    almost every business lands between 0.90 and 1.00. ``review_count`` carries
    nearly all of ``business_signal``'s discriminating power, which is why a run
    without review counts is worth flagging rather than quietly scoring.
    """
    span = RATING_CEILING - RATING_FLOOR
    return _clamp((rating - RATING_FLOOR) / span)


def review_signal(review_count: int) -> float:
    """Normalise a review count onto 0–1 on a log curve."""
    if review_count <= 0:
        return 0.0
    return _clamp(math.log10(1 + review_count) / math.log10(1 + REVIEW_SATURATION))


# How much of ``business_signal``'s 10 points applies when only one of its two
# inputs was published. Not a fudge factor: half the term's evidence is present,
# so half its weight applies and the rest is renormalised away like any other
# missing input.
PARTIAL_SIGNAL_WEIGHT = 0.5


def business_signal(
    rating: float | None, review_count: int | None
) -> tuple[float | None, BusinessSignalBasis, float]:
    """§10.2's rating/reviews term as ``(value, basis, weight_fraction)``.

    ``value`` is ``None`` when the source published neither input, which
    propagates to the score as a fully omitted term. It is never coerced to 0.0 —
    see this module's docstring.
    """
    if rating is not None and review_count is not None:
        value = 0.5 * rating_signal(rating) + 0.5 * review_signal(review_count)
        return value, BusinessSignalBasis.RATING_AND_REVIEWS, 1.0
    if rating is not None:
        return rating_signal(rating), BusinessSignalBasis.RATING_ONLY, PARTIAL_SIGNAL_WEIGHT
    if review_count is not None:
        return (
            review_signal(review_count),
            BusinessSignalBasis.REVIEWS_ONLY,
            PARTIAL_SIGNAL_WEIGHT,
        )
    return None, BusinessSignalBasis.NONE, 0.0


def completeness(signals: LeadSignals) -> float:
    """Ratio of ``COMPLETENESS_FIELDS`` that are populated."""
    present = sum(1 for name in COMPLETENESS_FIELDS if getattr(signals, name))
    return present / len(COMPLETENESS_FIELDS)


def score_lead(signals: LeadSignals) -> LeadScore:
    """Apply §10.2, renormalising over the weight whose inputs actually exist."""
    signal_value, basis, signal_weight = business_signal(signals.rating, signals.review_count)

    # term -> (value in 0–1, fraction of the term's weight that applies)
    values: dict[ScoreTerm, tuple[float, float] | None] = {
        ScoreTerm.WHATSAPP_EVIDENCE: (_clamp(signals.whatsapp_evidence), 1.0),
        ScoreTerm.CONTACT_CONFIDENCE: (_clamp(signals.contact_confidence), 1.0),
        ScoreTerm.PERSON_ATTRIBUTION: (_clamp(signals.person_attribution), 1.0),
        ScoreTerm.SOURCE_AGREEMENT: (1.0 if signals.n_sources >= 2 else 0.0, 1.0),
        ScoreTerm.BUSINESS_SIGNAL: (
            None if signal_value is None else (signal_value, signal_weight)
        ),
        ScoreTerm.COMPLETENESS: (completeness(signals), 1.0),
    }

    present = {term: pair for term, pair in values.items() if pair is not None}
    applied = {term: value for term, (value, _) in present.items()}
    omitted = tuple(term for term, pair in values.items() if pair is None)
    applicable_weight = sum(WEIGHTS[term] * fraction for term, (_, fraction) in present.items())

    if applicable_weight == 0:
        # Unreachable while any term is unconditionally present, but a score of 0
        # is the honest answer to "we could evaluate nothing" and it beats a
        # ZeroDivisionError three stages downstream.
        return LeadScore(
            score=0,
            omitted=omitted,
            applicable_weight=0.0,
            business_signal_basis=basis,
            has_mobile=signals.has_mobile,
        )

    earned = sum(
        WEIGHTS[term] * fraction * value
        for term, (value, fraction) in present.items()
    )
    return LeadScore(
        score=round(TOTAL_WEIGHT * earned / applicable_weight),
        terms=applied,
        omitted=omitted,
        applicable_weight=applicable_weight,
        business_signal_basis=basis,
        has_mobile=signals.has_mobile,
    )


def score_ceiling(unavailable: tuple[ScoreTerm, ...]) -> int:
    """The highest score attainable while ``unavailable`` terms score 0.

    Used to report honestly that Phase 4's table tops out at 85 because §8's
    attribution engine does not exist yet, rather than letting an operator read
    "no row scores above 85" as a property of the businesses.
    """
    return TOTAL_WEIGHT - sum(WEIGHTS[term] for term in unavailable)

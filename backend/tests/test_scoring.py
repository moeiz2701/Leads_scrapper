"""§10.2 lead scoring — pure, no DB.

The load-bearing property here is the one §5.1 spent a correction on: a field the
source never published must not be scored as a zero.
"""

from __future__ import annotations

import pytest

from leadscraper.core.scoring import (
    COMPLETENESS_FIELDS,
    QUALIFIED_SCORE,
    TOTAL_WEIGHT,
    WEIGHTS,
    BusinessSignalBasis,
    LeadSignals,
    ScoreTerm,
    business_signal,
    rating_signal,
    review_signal,
    score_ceiling,
    score_lead,
)


def _full(**overrides) -> LeadSignals:
    """A business with every §10.2 input at its best."""
    base = dict(
        whatsapp_evidence=1.0,
        contact_confidence=1.0,
        person_attribution=1.0,
        n_sources=2,
        rating=5.0,
        review_count=1000,
        has_address=True,
        has_online_presence=True,
        has_email=True,
        has_second_phone=True,
        has_mobile=True,
    )
    return LeadSignals(**{**base, **overrides})


# --------------------------------------------------------------------------- #
# The formula
# --------------------------------------------------------------------------- #


def test_weights_are_the_ones_printed_in_the_doc() -> None:
    assert WEIGHTS[ScoreTerm.WHATSAPP_EVIDENCE] == 30
    assert WEIGHTS[ScoreTerm.CONTACT_CONFIDENCE] == 25
    assert WEIGHTS[ScoreTerm.PERSON_ATTRIBUTION] == 15
    assert WEIGHTS[ScoreTerm.SOURCE_AGREEMENT] == 10
    assert WEIGHTS[ScoreTerm.BUSINESS_SIGNAL] == 10
    assert WEIGHTS[ScoreTerm.COMPLETENESS] == 10
    assert TOTAL_WEIGHT == 100


def test_a_perfect_lead_scores_100() -> None:
    result = score_lead(_full())
    assert result.score == 100
    assert result.omitted == ()
    assert result.applicable_weight == 100
    assert result.is_qualified


def test_an_empty_record_scores_0() -> None:
    result = score_lead(LeadSignals())
    assert result.score == 0
    assert not result.is_qualified


def test_terms_are_clamped_not_trusted() -> None:
    """A source that hands back 1.4 must not buy 42 points."""
    assert score_lead(_full(whatsapp_evidence=1.4)).score == 100
    assert score_lead(LeadSignals(whatsapp_evidence=-3.0)).score == 0


# --------------------------------------------------------------------------- #
# Missing stays missing (§5.1, and the reason this phase exists)
# --------------------------------------------------------------------------- #


def test_missing_review_count_does_not_score_as_zero_reviews() -> None:
    """§5.1: "a fabricated 0 would push good leads *down* the ranking."

    Measured stakes: ``review_count`` is present on 80% of the Islamabad run and
    **0%** of the Lahore one. Coercing missing to 0 would rank every Lahore
    business below every Islamabad business for a payload artefact.
    """
    unknown = score_lead(_full(review_count=None))
    fabricated_zero = score_lead(_full(review_count=0))
    assert unknown.score > fabricated_zero.score
    assert unknown.business_signal_basis is BusinessSignalBasis.RATING_ONLY


def test_a_business_with_no_rating_data_is_renormalised_not_docked() -> None:
    """The 10 points are removed from the denominator, not from the numerator."""
    result = score_lead(_full(rating=None, review_count=None))
    assert result.omitted == (ScoreTerm.BUSINESS_SIGNAL,)
    assert result.applicable_weight == 90
    # Everything else is perfect, so the score must still be full marks.
    assert result.score == 100
    assert result.business_signal_basis is BusinessSignalBasis.NONE


def test_renormalising_preserves_the_ranking_of_two_otherwise_equal_leads() -> None:
    """Two identical businesses, one of which Maps happened to describe less
    richly, must not be separated by that fact alone."""
    rich = score_lead(_full(whatsapp_evidence=0.6, rating=4.6, review_count=31))
    thin = score_lead(_full(whatsapp_evidence=0.6, rating=None, review_count=None))
    assert abs(rich.score - thin.score) <= 5


def test_business_signal_records_which_inputs_it_used() -> None:
    assert business_signal(4.5, 100)[1] is BusinessSignalBasis.RATING_AND_REVIEWS
    assert business_signal(4.5, None)[1] is BusinessSignalBasis.RATING_ONLY
    assert business_signal(None, 100)[1] is BusinessSignalBasis.REVIEWS_ONLY
    assert business_signal(None, None) == (None, BusinessSignalBasis.NONE, 0.0)


def test_one_input_carries_half_the_terms_weight() -> None:
    """Both inputs present is full weight; one is half; neither is omitted."""
    assert business_signal(4.5, 100)[2] == 1.0
    assert business_signal(4.5, None)[2] == 0.5
    assert business_signal(None, 100)[2] == 0.5
    assert business_signal(None, None)[2] == 0.0

    assert score_lead(_full()).applicable_weight == 100
    assert score_lead(_full(review_count=None)).applicable_weight == 95
    assert score_lead(_full(rating=None, review_count=None)).applicable_weight == 90


def test_a_run_without_review_counts_is_not_flattered_by_the_gap() -> None:
    """The §5.1 warning, inverted — and measured on the live runs.

    PK salon ratings cluster at 4.5–5.0 (Islamabad median 4.6) while review
    counts spread widely (median 31). So rating alone normalises to ~0.90 where
    rating-and-reviews normalises to ~0.78 for the *same* business. At full
    weight the Lahore run, whose payload carried no review counts at all, would
    score systematically above Islamabad for a payload artefact. Half weight plus
    renormalisation keeps them within a point of each other.
    """
    typical = dict(whatsapp_evidence=0.60, contact_confidence=0.85, n_sources=1,
                   has_address=True, has_online_presence=True, has_mobile=True)
    islamabad = score_lead(LeadSignals(rating=4.6, review_count=31, **typical))
    lahore = score_lead(LeadSignals(rating=4.7, review_count=None, **typical))
    assert abs(islamabad.score - lahore.score) <= 1


# --------------------------------------------------------------------------- #
# person_attribution is scored, not omitted (§8 / Phase 9)
# --------------------------------------------------------------------------- #


def test_an_unattributed_business_is_scored_zero_on_the_person_term() -> None:
    """Not omitted, unlike business_signal — and the distinction is deliberate.

    Most PK SMB salons genuinely have no publicly named owner, so "no named
    person" is a true statement about the record rather than a hole in it.
    Renormalising it away would also hide the fact that §8's engine is Phase 9,
    and every §16 weight tuned against the inflated scale would be wrong.
    """
    result = score_lead(_full(person_attribution=0.0))
    assert ScoreTerm.PERSON_ATTRIBUTION not in result.omitted
    assert result.applicable_weight == 100
    assert result.score == 85


def test_the_reported_ceiling_explains_a_table_that_stops_at_85() -> None:
    assert score_ceiling((ScoreTerm.PERSON_ATTRIBUTION,)) == 85
    assert score_ceiling(()) == 100


# --------------------------------------------------------------------------- #
# completeness (§10.2) must not punish the shape of the market
# --------------------------------------------------------------------------- #


def test_completeness_scores_online_presence_not_website_ownership() -> None:
    """Only 32% of discovered businesses have a website (§5.1, §14) — that is the
    shape of the PK SMB market, not a defect in the lead. Scoring ``has_website``
    directly would dock two thirds of every run for it. The field is a
    disjunction over website/Facebook/Instagram instead, so a salon reachable
    only on Instagram scores the same as one with a domain. That the disjunction
    is built correctly is pinned in test_scoring_service.py against real rows."""
    assert "has_website" not in COMPLETENESS_FIELDS
    assert "has_online_presence" in COMPLETENESS_FIELDS


def test_completeness_is_the_populated_ratio_of_its_field_set() -> None:
    # rating/reviews supplied so business_signal applies and the denominator
    # stays at 100 — otherwise the renormalisation rescales the gap.
    bare = LeadSignals(rating=1.0, review_count=0)
    none_set = score_lead(bare)
    all_set = score_lead(
        LeadSignals(
            rating=1.0,
            review_count=0,
            has_address=True,
            has_online_presence=True,
            has_email=True,
            has_second_phone=True,
        )
    )
    assert len(COMPLETENESS_FIELDS) == 4
    assert none_set.applicable_weight == 100
    assert all_set.score - none_set.score == WEIGHTS[ScoreTerm.COMPLETENESS]

    half = score_lead(LeadSignals(rating=1.0, review_count=0, has_address=True,
                                  has_email=True))
    assert half.score == WEIGHTS[ScoreTerm.COMPLETENESS] // 2


def test_completeness_does_not_include_area_or_geo() -> None:
    """Both are 100% filled on Maps data, so scoring them would add a constant
    to every row and buy no discrimination."""
    assert "has_area" not in COMPLETENESS_FIELDS
    assert not any("lat" in name or "lng" in name for name in COMPLETENESS_FIELDS)


# --------------------------------------------------------------------------- #
# The individual signals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("rating", "expected"),
    [(5.0, 1.0), (1.0, 0.0), (3.0, 0.5), (4.6, 0.9)],
)
def test_rating_signal_spans_the_1_to_5_scale(rating: float, expected: float) -> None:
    assert rating_signal(rating) == pytest.approx(expected, abs=1e-9)


def test_review_signal_is_logarithmic_and_saturates() -> None:
    assert review_signal(0) == 0.0
    assert review_signal(200) == pytest.approx(1.0)
    assert review_signal(6488) == 1.0
    # The Islamabad median of 31 must land mid-range, not in the basement — a
    # linear map would put 95% of that run in the bottom 5% of the scale.
    assert 0.55 < review_signal(31) < 0.75


def test_source_agreement_is_binary_at_two() -> None:
    """§10.2 says "n_sources ≥ 2 → 1.0" and does not grade beyond it."""
    def at(n: int) -> int:
        return score_lead(LeadSignals(n_sources=n, rating=1.0, review_count=0)).score

    assert at(0) == at(1) == 0
    assert at(2) == at(3) == at(9) == WEIGHTS[ScoreTerm.SOURCE_AGREEMENT]


# --------------------------------------------------------------------------- #
# Qualification (§10.2)
# --------------------------------------------------------------------------- #


def test_qualified_needs_both_the_score_and_a_mobile() -> None:
    """§10.2: "Good quality lead" = score ≥ 60 **and** at least one mobile.

    A landline-only business can clear 60 on evidence alone, and it is still not
    the lead §10.2 defines — you cannot WhatsApp it.
    """
    landline_only = score_lead(_full(has_mobile=False))
    assert landline_only.score >= QUALIFIED_SCORE
    assert not landline_only.is_qualified
    assert score_lead(_full(has_mobile=True)).is_qualified

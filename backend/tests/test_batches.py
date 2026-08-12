"""The outreach cascade — _BATCH_SPEC.md, pinned step by step.

Two properties carry this file, and both are about what the cascade *promises*
rather than what it computes:

* **Exhaustive and mutually exclusive.** Every business gets exactly one batch.
  The spec's whole reason for being a cascade rather than six filters is that a
  business must never be messaged twice, and overlapping sets are how that
  happens. ``test_the_cascade_order_is_load_bearing`` is the test that would fail
  if someone reordered the steps to read more naturally.
* **It only claims what it has measured.** The thresholds came off one Lahore ×
  food scrape, so ``food`` is the only category it routes. Everything else is
  ``unbatched`` — not a fallback batch, not a best guess.

The boundary tests (exactly 200 reviews, exactly 4.0) are here because both
thresholds are ``<`` in the spec and both are the kind of comparison that gets
"tidied" into ``<=`` by someone who has not read §3.
"""

from __future__ import annotations

import pytest

from leadscraper.core import batches
from leadscraper.core.batches import (
    BATCHES,
    UNBATCHED,
    Assignment,
    assign,
    cascade,
    clean_number,
    is_dine_in,
    pick_whatsapp,
)
from leadscraper.enums import BelongsTo, ContactKind, LineType, WhatsAppLabel
from leadscraper.services.extraction import EXTRACTABLE_LABELS


class _Business:
    """The five columns the cascade reads. Duck-typed, like ``export/rows.py``."""

    def __init__(
        self,
        *,
        category="food",
        subcategory="Restaurant",
        review_count=500,
        rating=4.5,
        website=None,
    ):
        self.category = category
        self.subcategory = subcategory
        self.review_count = review_count
        self.rating = rating
        self.website = website


class _Phone:
    def __init__(
        self,
        *,
        value_e164="+923001234567",
        wa_label=WhatsAppLabel.LIKELY,
        line_type=LineType.MOBILE,
        belongs_to=None,
        rank=1,
        kind=ContactKind.PHONE,
        value_raw="0300 1234567",
    ):
        self.kind = kind
        self.value_e164 = value_e164
        self.value_raw = value_raw
        self.wa_label = wa_label
        self.line_type = line_type
        self.belongs_to = belongs_to
        self.rank = rank


WA = [_Phone()]  # one `likely` mobile — the shape most rows in the live data have


# --------------------------------------------------------------------------- #
# §3 The cascade
# --------------------------------------------------------------------------- #


def test_the_cascade_order_is_load_bearing():
    """First match wins, and the steps are not independent predicates.

    This business qualifies for four steps at once: it has no WhatsApp number,
    under 200 reviews, a rating below 4.0, and it is a café with a site. Read as
    six filters it belongs to four batches; read as §3's cascade it belongs to
    exactly one, and it is the *first* one. Reordering the steps to group the
    "quality" checks together would silently start messaging the unreachable.
    """
    business = _Business(
        subcategory="Cafe", review_count=12, rating=2.1, website="https://x.pk"
    )
    assert assign(business, []).batch == batches.NO_WHATSAPP


def test_every_business_lands_in_exactly_one_batch():
    """Exhaustive: there is no input the cascade has no answer for.

    Sweeping the corners of all five inputs, every combination resolves, and it
    resolves to a slug the catalogue knows about. A cascade with a hole in it
    would drop businesses out of every batch and out of the outreach entirely —
    §5.5's silent zero, wearing a different hat.
    """
    seen = set()
    for has_whatsapp in (True, False):
        for review_count in (None, 0, 199, 200, 5_000):
            for rating in (None, 1.0, 3.9, 4.0, 5.0):
                for dine_in in (True, False):
                    for site in (True, False):
                        slug = cascade(
                            has_whatsapp=has_whatsapp,
                            review_count=review_count,
                            rating=rating,
                            dine_in=dine_in,
                            site=site,
                        )
                        assert slug in batches.SLUGS
                        seen.add(slug)
    # And every batch is reachable — one nobody can land in is a dead definition.
    assert seen == set(batches.SLUGS)


def test_no_whatsapp_number_outranks_every_other_signal():
    """§3 step 1. 5,000 reviews and a 4.9 rating do not make a business sendable.

    B00 had the *highest* median review count in the file (1,407) precisely
    because UAN lines signal established multi-branch operators. It is not a
    quality judgement and these rows are not deletable — they are routed to email
    or a visit instead.
    """
    business = _Business(review_count=5_000, rating=4.9)
    assert assign(business, []).batch == batches.NO_WHATSAPP


def test_an_unknown_review_count_is_treated_as_low_volume():
    """§3's null rule, and the one place this codebase does *not* apply "missing
    is not zero".

    §10.2 keeps a missing ``review_count`` out of the score entirely. Here the
    question is different: it is not "how good is this lead" but "which message
    do we send", and the `early-stage` message — Google listing, menu online, a
    WhatsApp link — is the honest one to send a business whose volume we cannot
    establish. The 21 null rows in the Lahore file land here by design.
    """
    assert assign(_Business(review_count=None), WA).batch == batches.EARLY_STAGE


def test_an_unknown_rating_skips_the_reputation_step_rather_than_failing_it():
    """§3's other null rule, which points the opposite way — deliberately.

    "No rating published" is not "rated below 4.0". `reputation`'s message sells
    a fix for a reputation problem, and sending it to a business whose rating we
    simply never scraped invents the problem. So a null rating *continues* down
    the cascade and this restaurant is routed on its type and its site instead.
    """
    business = _Business(rating=None, website="https://x.pk")
    assert assign(business, WA).batch == batches.DELIVERY_SITE


@pytest.mark.parametrize(
    "review_count,expected",
    [(199, batches.EARLY_STAGE), (200, batches.DELIVERY_NOSITE)],
)
def test_the_volume_threshold_is_exclusive(review_count, expected):
    """§1: ``review_count < 200`` is early stage. Exactly 200 is not."""
    assert assign(_Business(review_count=review_count), WA).batch == expected


@pytest.mark.parametrize(
    "rating,expected",
    [(3.9, batches.REPUTATION), (4.0, batches.DELIVERY_NOSITE)],
)
def test_the_rating_threshold_is_exclusive(rating, expected):
    """§1: ``rating < 4.0`` is the reputation track. Exactly 4.0 is not.

    Worth pinning because the failure is invisible and expensive: a 4.0 business
    tipped into `reputation` gets a message about intercepting complaints, and
    §5 of the spec says referencing the rating ends the conversation.
    """
    assert assign(_Business(rating=rating), WA).batch == expected


@pytest.mark.parametrize(
    "subcategory,website,expected",
    [
        ("Cafe", "https://x.pk", batches.CAFE_SITE),
        ("Cafe", None, batches.CAFE_NOSITE),
        ("Fast food restaurant", "https://x.pk", batches.DELIVERY_SITE),
        ("Fast food restaurant", None, batches.DELIVERY_NOSITE),
    ],
)
def test_the_dine_in_and_site_split(subcategory, website, expected):
    """§3 steps 4–7. Two binary splits, four batches, no overlap."""
    business = _Business(subcategory=subcategory, website=website)
    assert assign(business, WA).batch == expected


def test_an_empty_website_string_is_no_website():
    """``website`` is gap-filled from §5.1's payload and §6.4's bio link, and a
    blank that survived one of those is the absence of a site — not a site."""
    assert assign(_Business(website="   "), WA).batch == batches.DELIVERY_NOSITE


@pytest.mark.parametrize(
    "subcategory", ["Cafe", "cafe", "  CAFE  ", "Café", "Coffee shop"]
)
def test_the_dine_in_list_matches_case_and_accents(subcategory):
    """A business must not change batch because Maps wrote an acute accent.

    The two batches carry completely different offers — a café gets a menu and a
    booking flow, a delivery restaurant gets a commission argument — so an
    accent-sensitive comparison is an invisible misroute rather than a cosmetic
    bug.
    """
    assert is_dine_in(subcategory) is True


def test_an_unknown_subcategory_is_delivery_capable_not_dine_in():
    """§1: "everything else in the food category is treated as delivery-capable".

    The one place the cascade treats absent data as a value, and it is the
    spec's stated default rather than an inference of ours.
    """
    assert is_dine_in(None) is False
    assert is_dine_in("Something Maps invented last week") is False


# --------------------------------------------------------------------------- #
# The category scope
# --------------------------------------------------------------------------- #


def test_a_non_food_business_is_unbatched_rather_than_routed_by_foods_rules():
    """The cascade is calibrated on food and says so (Aug 2026).

    This salon would sail through every step — 800 reviews, 4.7, a WhatsApp
    number, and a subcategory that is not in the dine-in list — and land in
    `delivery-nosite`, whose message argues about Foodpanda's commission. The
    guard is before the cascade, not a fallback after it: a plausible wrong
    answer is worse than no answer, which is §5.5 applied to a segmentation.
    """
    salon = _Business(category="salon", subcategory="Beauty salon", review_count=800)
    assert assign(salon, WA).batch == UNBATCHED


def test_a_business_with_no_category_is_unbatched():
    assert assign(_Business(category=None), WA).batch == UNBATCHED


def test_unbatched_is_not_a_batch():
    """It is a filter token and a stored value, never an entry in the catalogue.

    A picker that listed it beside `delivery-nosite` would imply there is a
    message to send it. There is not — there is a vertical nobody has measured.
    """
    assert UNBATCHED not in batches.SLUGS
    assert UNBATCHED not in batches.BY_SLUG
    assert UNBATCHED in batches.FILTER_TOKENS
    assert batches.resolve(UNBATCHED) is None
    assert batches.resolve_token(UNBATCHED) == UNBATCHED


def test_the_derived_number_is_still_computed_outside_food():
    """"Which number would I message this on" is a question about a phone, not
    about a vertical. Only the routing is food-specific."""
    result = assign(_Business(category="salon"), WA)
    assert result.batch == UNBATCHED
    assert result.wa_number == "+923001234567"
    assert result.wa_confidence == WhatsAppLabel.LIKELY


# --------------------------------------------------------------------------- #
# §2 The derived WhatsApp number
# --------------------------------------------------------------------------- #


def test_the_highest_scoring_qualifying_number_wins():
    """§2's table: confirmed +2, likely +1, mobile +1, business line +1."""
    phones = [
        _Phone(value_e164="+923001111111", wa_label=WhatsAppLabel.LIKELY, rank=1),
        _Phone(
            value_e164="+923002222222",
            wa_label=WhatsAppLabel.CONFIRMED,
            belongs_to=BelongsTo.BUSINESS,
            rank=2,
        ),
    ]
    pick = pick_whatsapp(phones)
    assert pick is not None
    assert pick.number == "+923002222222"
    assert pick.pick_score == 4


def test_a_tie_breaks_on_rank_rather_than_on_row_order():
    """Two equally-scored numbers resolve to the one §3.3 already chose.

    Otherwise the number a business gets messaged on depends on the order the
    database happened to return its contacts in, which is not a decision anybody
    made.
    """
    phones = [
        _Phone(value_e164="+923009999999", rank=3),
        _Phone(value_e164="+923001111111", rank=1),
    ]
    assert pick_whatsapp(phones).number == "+923001111111"


def test_a_no_label_never_qualifies():
    """§9.3's `no` is the record arguing against the number, not an unknown."""
    phones = [_Phone(wa_label=WhatsAppLabel.NO), _Phone(wa_label=None, rank=2)]
    assert pick_whatsapp(phones) is None


def test_an_unranked_number_is_not_pickable():
    """``rank is None`` is a §3.3 exclusion or a §10.1 duplicate provenance row —
    either way it is not a number the operator can see in the table."""
    assert pick_whatsapp([_Phone(rank=None)]) is None


def test_the_fifth_number_counts_even_though_the_export_shows_four():
    """The documented departure from §2, which scans ``phone_1``…``phone_4``.

    §12.1's four slots are a column-set cap and §10.1 forbids one becoming a data
    cap. Under a literal reading this business is `no-whatsapp` — while the
    clipboard pull, reading the same ranked set, would happily hand out its
    number. One of the two would be lying.
    """
    phones = [
        _Phone(value_e164=f"+92300000000{i}", wa_label=WhatsAppLabel.NO, rank=i)
        for i in range(1, 5)
    ]
    phones.append(_Phone(value_e164="+923005555555", rank=5))

    assert pick_whatsapp(phones).number == "+923005555555"
    assert assign(_Business(), phones).batch == batches.DELIVERY_NOSITE


def test_the_batch_boundary_and_the_clipboard_read_the_same_labels():
    """`no-whatsapp` is defined as "the pull would yield nothing here".

    Two frozensets that drifted apart would put businesses in a send batch the
    clipboard then skips, or file a messageable business under Unreachable. So
    there is one object, imported.
    """
    assert EXTRACTABLE_LABELS is batches.WA_VALID_LABELS


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('="+923005326559"', "+923005326559"),  # §2's Excel-escaped source value
        ("0300 532 6559", "03005326559"),
        ("+92 (300) 532-6559", "+923005326559"),
        ("1122", None),  # a shortcode is not a number
        ("", None),
        (None, None),
    ],
)
def test_number_cleaning(raw, expected):
    """§2's ``clean_num``. Too short returns ``None``, never a repaired fragment:
    passing a shortened string on would be a guess at what the source meant."""
    assert clean_number(raw) == expected


def test_an_email_is_never_picked_as_a_whatsapp_number():
    contacts = [
        _Phone(kind=ContactKind.EMAIL, value_e164=None, value_raw="hi@salon.pk"),
        _Phone(value_e164="+923001111111"),
    ]
    assert pick_whatsapp(contacts).number == "+923001111111"


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


def test_the_catalogue_is_internally_consistent():
    """Slugs and ids are unique, and the send order is 1–6 with one exception.

    `no-whatsapp` has no send priority — not a priority of "last". It is not in
    the send order at all, and modelling it as 7 would eventually get it sent.
    """
    assert len({b.slug for b in BATCHES}) == len(BATCHES)
    assert len({b.id for b in BATCHES}) == len(BATCHES)

    priorities = [b.send_priority for b in BATCHES if b.sendable]
    assert sorted(priorities) == [1, 2, 3, 4, 5, 6]
    assert [b.slug for b in BATCHES if not b.sendable] == [batches.NO_WHATSAPP]
    assert [b.send_priority for b in BATCHES if not b.sendable] == [None]


def test_the_catalogue_is_listed_in_send_priority_order():
    """The picker offers them in the order the operator works them, which is why
    B05 and B06 are out of numerical order — §4's table, not the id sequence."""
    sendable = [b for b in BATCHES if b.sendable]
    assert [b.send_priority for b in sendable] == sorted(
        b.send_priority for b in sendable
    )
    assert [b.id for b in BATCHES] == ["B01", "B02", "B03", "B04", "B06", "B05", "B00"]


@pytest.mark.parametrize("token", ["B01", "b01", "delivery-nosite", " DELIVERY-NOSITE "])
def test_both_spellings_resolve_to_one_token(token):
    """The URL carries slugs and every conversation about the spec says "B01"."""
    assert batches.resolve_token(token) == batches.DELIVERY_NOSITE


def test_an_unknown_token_resolves_to_nothing_rather_than_to_a_default():
    assert batches.resolve_token("B99") is None
    assert batches.resolve_token("cafe") is None


def test_the_assignment_exposes_its_definition():
    result = Assignment(batch=batches.CAFE_SITE, wa_number=None, wa_confidence=None)
    assert result.spec is not None
    assert result.spec.id == "B04"
    assert result.sendable is True
    # ...and `unbatched` has none, without raising.
    assert Assignment(UNBATCHED, None, None).spec is None
    assert Assignment(UNBATCHED, None, None).sendable is False

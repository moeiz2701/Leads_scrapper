"""Outreach batches — [_BATCH_SPEC.md](../../../../_BATCH_SPEC.md), as a pure function.

The §13 filter bar splits the table on *properties* — a WhatsApp label, a phone
type, a website. A batch is the other question: **which message does this
business get?** The spec answers it with a cascade over six of those properties
at once, and the cascade's whole point is that it is **exhaustive and mutually
exclusive** — every business resolves to exactly one batch, so nobody is
messaged twice and nobody is silently dropped.

That is a stronger promise than a filter makes, and it is why this is one
function rather than six filter clauses that happen to be combined in the UI:
``delivery-nosite`` is not "no website AND category food AND review_count ≥ 200"
typed into a filter bar. It is "everything the first three cascade steps did not
already claim, that is delivery-capable and has no site". Reordering the steps
or evaluating them independently produces overlapping sets, which is exactly the
double-messaging the spec exists to prevent — ``test_batches.py`` pins the order.

**The cascade is defined for ``food`` and for nothing else** (Aug 2026). Every
threshold in it was calibrated on one Lahore × food scrape, and the two branches
that carry the most weight are food-shaped: ``DINE_IN_SUBCATEGORIES`` is a list
of restaurant subcategories, and the delivery split exists because of a
25–35% Foodpanda commission. Run a salon through it and every row lands in a
`delivery-*` batch — a real-looking label, a send priority, and an offer about
delivery commission aimed at a hair salon. So a business outside ``BATCHED_CATEGORIES``
resolves to ``UNBATCHED``, which is not a batch: it has no message, no send
priority, and it is never a send target. That is §5.5's rule applied to a
segmentation — a layer with nothing to say says so, rather than returning a
plausible answer. Defining the other six verticals is a measurement exercise per
§8 of the spec, not a matter of widening this list.

**Three departures from the spec, each deliberate:**

* **The spec scans ``phone_1``…``phone_4``; this scans every ranked phone.**
  §12.1's four slots are a *column-set* cap and §10.1 forbids letting one become
  a data cap. A business whose only WhatsApp-capable number is its fifth would
  land in ``no-whatsapp`` under a literal reading — while ``services/extraction``
  would happily put that number on the clipboard, because it reads the same
  ranked set this does. One of the two would be lying; they read the same set so
  neither is.
* **``wa_confidence`` is §9.3's label, not the pick score.** The score below
  exists to *choose* between two qualifying numbers and nothing else. Exporting a
  0–4 integer next to a phone number invites reading it as a confidence
  percentage, and the standing rule is that the operator sees
  ``confirmed``/``likely`` and the raw evidence stays internal.
* **The spec's ``clean_num`` is kept but is a guard, not a repair.** Its input is
  a CSV cell Excel escaped to ``="+923005326559"``; ours is ``contacts.value_e164``,
  already E.164 from §9.1. It stays because ``value_raw`` is the fallback when a
  number never parsed, and a 4-digit shortcode reaching a bulk sender is the
  failure the spec calls out.

Pure: no DB, no network, no imports from ``services``. ``business`` is duck-typed
for the same reason ``export/rows.py`` duck-types it — the caller passes an ORM
row, the tests pass a stub, and this module knows about neither.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from leadscraper.enums import (
    BelongsTo,
    Category,
    ContactKind,
    LineType,
    WhatsAppLabel,
)

# --------------------------------------------------------------------------- #
# §1 Constants
# --------------------------------------------------------------------------- #

# §9.3's labels the public record supports messaging on. `no` is not "unknown" —
# it is the record arguing against the number — so it is the one label that
# cannot make a business sendable.
#
# ``services.extraction.EXTRACTABLE_LABELS`` **is** this set, imported rather
# than restated: if the two ever diverged, `no-whatsapp` would hold businesses
# whose clipboard pull yields numbers, or a sendable batch would hold businesses
# it yields nothing for. ``test_batches.py`` pins the identity.
WA_VALID_LABELS: Final[frozenset[str]] = frozenset(
    {WhatsAppLabel.CONFIRMED, WhatsAppLabel.LIKELY}
)

# The categories the cascade has definitions for. **One**, and widening it is a
# measurement, not an edit** — see the module docstring. §4's seven verticals
# each need their own thresholds and their own equivalent of the dine-in list;
# until one has them, its businesses are `UNBATCHED` rather than routed by
# food's rules.
BATCHED_CATEGORIES: Final[frozenset[str]] = frozenset({Category.FOOD})

# Not a batch. The reserved token for "the cascade has no definition covering
# this business" — a category outside `BATCHED_CATEGORIES`, or a business whose
# category was never recorded. It is a filter token and a stored value so that
# "which rows does the batch layer have no opinion on?" is answerable, and it is
# deliberately not a member of ``BATCHES``: it has no message and no send
# priority, and a picker that listed it alongside `delivery-nosite` would imply
# there is something to send it.
UNBATCHED: Final[str] = "unbatched"

# §1. Calibrated to the Lahore × food dataset (median 736 reviews); §8 of the
# spec says to recompute them as the 25th/75th percentile for a smaller city
# rather than carrying them over. They are module constants and not settings
# because changing them re-partitions every batch — that is a decision with a
# measurement behind it, not a knob.
VOLUME_THRESHOLD: Final[int] = 200
RATING_THRESHOLD: Final[float] = 4.0

# §2's `clean_num` floor. A PK mobile in E.164 is 13 characters; 10 is the
# spec's number and is loose on purpose, so a legitimately short landline is
# kept while a 4-digit shortcode or a stray fragment is not.
MIN_NUMBER_LENGTH: Final[int] = 10

_KEEP_NUMBER = re.compile(r"[^0-9+]")
_WHITESPACE = re.compile(r"\s+")

# §1, casefolded at import so the lookup is exact rather than a scan. Maps
# publishes these as its own subcategory strings — "Coffee shop", "Dessert
# restaurant" — and everything else in the food category is treated as
# delivery-capable, which is the assumption the whole delivery/dine-in split
# rests on.
DINE_IN_SUBCATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "art cafe",
        "bakery",
        "bar",
        "buffet restaurant",
        "cafe",
        "coffee shop",
        "dessert restaurant",
        "dessert shop",
        "hookah bar",
        "ice cream shop",
        "steak house",
    }
)


# --------------------------------------------------------------------------- #
# §4 The batches
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Batch:
    """One batch of §4's table.

    ``slug`` is the identifier everything outside this module uses — the query
    string, the ledger column, the UI. ``id`` is the spec's ``B01``…``B06``,
    carried so a conversation about the spec and a row on screen name the same
    thing, and deliberately *not* used as the wire value: ``B05``/``B06`` are out
    of cascade order and a sorted list of ids would read as a priority it is not.
    """

    id: str
    slug: str
    name: str
    definition: str
    # §4's send priority. ``None`` for `no-whatsapp`, which is not a send order
    # of "last" — it is not in the send order at all.
    send_priority: int | None
    sendable: bool
    note: str


NO_WHATSAPP: Final[str] = "no-whatsapp"
EARLY_STAGE: Final[str] = "early-stage"
REPUTATION: Final[str] = "reputation"
CAFE_SITE: Final[str] = "cafe-site"
CAFE_NOSITE: Final[str] = "cafe-nosite"
DELIVERY_SITE: Final[str] = "delivery-site"
DELIVERY_NOSITE: Final[str] = "delivery-nosite"

# In §4's **send priority** order, not cascade order. This is the order the
# operator works the list in, so it is the order the picker offers.
BATCHES: Final[tuple[Batch, ...]] = (
    Batch(
        id="B01",
        slug=DELIVERY_NOSITE,
        name="Commission Escape",
        definition="Delivery-capable · 200+ reviews · rating ≥4.0 · no website",
        send_priority=1,
        sendable=True,
        note=(
            "Proven order volume, zero owned infrastructure, and a quantifiable "
            "pain (25–35% Foodpanda commission). Highest intent in the file."
        ),
    ),
    Batch(
        id="B02",
        slug=DELIVERY_SITE,
        name="Ordering Layer",
        definition="Delivery-capable · 200+ reviews · rating ≥4.0 · has website",
        send_priority=2,
        sendable=True,
        note=(
            "They already bought a site once, which proves budget — it just does "
            "not take orders. Pitch an addition, never a rebuild."
        ),
    ),
    Batch(
        id="B03",
        slug=CAFE_NOSITE,
        name="Café First Presence",
        definition="Dine-in/dessert · 200+ reviews · rating ≥4.0 · no website",
        send_priority=3,
        sendable=True,
        note=(
            "Real footfall, nothing online. Commission framing does not apply — "
            "these are dine-in, brand-led businesses."
        ),
    ),
    Batch(
        id="B04",
        slug=CAFE_SITE,
        name="Café Content & Booking",
        definition="Dine-in/dessert · 200+ reviews · rating ≥4.0 · has website",
        send_priority=4,
        sendable=True,
        note=(
            "The gap is conversion and freshness, not existence. Best retainer "
            "candidates: content and photography rather than a one-off build."
        ),
    ),
    Batch(
        id="B06",
        slug=REPUTATION,
        name="Feedback Loop",
        definition="200+ reviews · rating <4.0 · any type",
        send_priority=5,
        sendable=True,
        note=(
            "Never reference the rating — it reads as an insult and ends the "
            "thread. Sell the operational fix: post-order WhatsApp feedback."
        ),
    ),
    Batch(
        id="B05",
        slug=EARLY_STAGE,
        name="Starter Setup",
        definition="<200 reviews or unknown · any type",
        send_priority=6,
        sendable=True,
        note=(
            "Thin budgets and a high failure rate, so the honest recommendation "
            "is against a full site. Work it only after the first five."
        ),
    ),
    Batch(
        id="B00",
        slug=NO_WHATSAPP,
        name="Unreachable — Email/Visit",
        definition="No WhatsApp-capable number on any ranked phone",
        send_priority=None,
        sendable=False,
        note=(
            "Not a failure and not deletable: UAN lines signal established "
            "multi-branch operators, and this batch had the highest median "
            "review count of any. Route to email, Instagram DM or a visit."
        ),
    ),
)

BY_SLUG: Final[dict[str, Batch]] = {b.slug: b for b in BATCHES}
BY_ID: Final[dict[str, Batch]] = {b.id: b for b in BATCHES}
SLUGS: Final[tuple[str, ...]] = tuple(b.slug for b in BATCHES)

# Everything the ``batch`` filter accepts and everything ``batch_counts`` keys
# on: the seven batches plus the reserved ``unbatched``. Counts are keyed on
# this rather than on ``SLUGS`` so a view of salons reports "412 unbatched"
# instead of seven zeroes and no explanation.
FILTER_TOKENS: Final[tuple[str, ...]] = (*SLUGS, UNBATCHED)


def resolve(token: str) -> Batch | None:
    """A slug or a ``B0N`` id → its batch. Anything else is ``None``.

    Both spellings are accepted because both are in the operator's vocabulary:
    the URL carries slugs, and the spec — and therefore every conversation about
    it — says "B01". ``UNBATCHED`` resolves to ``None`` here: it is a valid
    *filter token* and not a batch, which is what ``resolve_token`` is for.
    """
    key = token.strip()
    return BY_SLUG.get(key.lower()) or BY_ID.get(key.upper())


def resolve_token(token: str) -> str | None:
    """A slug, a ``B0N`` id or ``unbatched`` → the canonical filter token.

    One vocabulary for the query string, the stored ledger value and the counts,
    so a batch never has two spellings that have to be kept in step.
    """
    key = token.strip().lower()
    if key == UNBATCHED:
        return UNBATCHED
    batch = resolve(token)
    return batch.slug if batch else None


def applies_to(category: str | None) -> bool:
    """Whether the cascade has definitions covering this category."""
    return bool(category) and str(category).strip().lower() in BATCHED_CATEGORIES


# --------------------------------------------------------------------------- #
# §2 Derived fields
# --------------------------------------------------------------------------- #


class BatchContact(Protocol):
    """The ``contacts`` columns the cascade reads."""

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
    def rank(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class WhatsAppPick:
    """The number this business gets messaged on, and why it won."""

    number: str
    # §9.3's label — `confirmed` or `likely`. This is `wa_confidence`.
    label: str
    # §2's tie-break score, 1–4. Internal: it ranks two qualifying numbers
    # against each other and means nothing on its own.
    pick_score: int


def clean_number(value: str | None) -> str | None:
    """§2's ``clean_num`` — digits and ``+`` only, or ``None`` if too short.

    Returns ``None`` rather than a shortened string: a number that fails this is
    not a number, and passing a repaired fragment on would be a guess at what the
    source meant. Missing stays missing.
    """
    if not value:
        return None
    cleaned = _KEEP_NUMBER.sub("", str(value))
    return cleaned if len(cleaned) >= MIN_NUMBER_LENGTH else None


def _pick_score(contact: BatchContact) -> int:
    """§2's table. Confirmed beats likely; a mobile beats a landline; a number
    known to be the business's beats one whose owner we never established."""
    score = 2 if contact.wa_label == WhatsAppLabel.CONFIRMED else 1
    if contact.line_type == LineType.MOBILE:
        score += 1
    if contact.belongs_to == BelongsTo.BUSINESS:
        score += 1
    return score


def pick_whatsapp(contacts: Iterable[BatchContact]) -> WhatsAppPick | None:
    """§2's ``wa_number`` / ``wa_confidence`` — the best messageable number.

    ``rank is None`` is excluded for the reason it is everywhere else: it means
    either §3.3's ``whatsapp_only`` filtered the number out or a §10.1 merge left
    a second provenance row on a number that already holds its place. Neither is
    a number the operator can see in the table.

    Ties break on rank, so two equally-scored numbers resolve to the one §3.3
    already decided was the business's best — not to whichever the database
    happened to return first.
    """
    best: WhatsAppPick | None = None
    best_rank = 0

    for contact in contacts:
        if contact.kind != ContactKind.PHONE or contact.rank is None:
            continue
        if contact.wa_label not in WA_VALID_LABELS:
            continue
        number = clean_number(contact.value_e164) or clean_number(contact.value_raw)
        if not number:
            continue

        score = _pick_score(contact)
        if best is None or score > best.pick_score or (
            score == best.pick_score and contact.rank < best_rank
        ):
            best = WhatsAppPick(
                number=number, label=str(contact.wa_label), pick_score=score
            )
            best_rank = contact.rank

    return best


def _fold(value: str) -> str:
    """Casefold, strip accents, collapse whitespace.

    "Café" and "Cafe" are the same subcategory, and a business landing in
    `delivery-nosite` because Maps wrote an acute accent would be an invisible
    misroute — the offer text for the two batches is completely different.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _WHITESPACE.sub(" ", stripped).strip().casefold()


def is_dine_in(subcategory: str | None) -> bool:
    """§2. A missing subcategory is **not** dine-in.

    That is an asymmetry worth naming: it is the one place the cascade treats
    absent data as a value. `DINE_IN_SUBCATEGORIES` is a closed list and
    "everything else is delivery-capable" is the spec's stated default, so an
    unknown subcategory falls to the default the same way an unlisted one does.
    """
    return bool(subcategory) and _fold(subcategory) in DINE_IN_SUBCATEGORIES


def has_site(website: str | None) -> bool:
    """§2's ``has_site``. An empty string is no site, not a site.

    ``website`` is gap-filled from several places — §5.1's Maps payload, §6.4's
    bio link — and a blank that survived one of those is the absence of a site.
    """
    return bool(website and website.strip())


# --------------------------------------------------------------------------- #
# §3 The cascade
# --------------------------------------------------------------------------- #


def cascade(
    *,
    has_whatsapp: bool,
    review_count: int | None,
    rating: float | None,
    dine_in: bool,
    site: bool,
) -> str:
    """§3, step for step. **First match wins — do not reorder.**

    Two null rules, and they differ on purpose:

    * A null ``review_count`` falls to `early-stage`. Unknown volume is treated
      as low volume — the one place this codebase deliberately does *not* apply
      "missing is not zero", because the spec makes it a routing decision rather
      than a measurement: the `early-stage` message is the honest one to send a
      business whose volume we cannot establish.
    * A null ``rating`` **skips** step 3 and continues. "No rating published" is
      not "rated below 4.0", and `reputation`'s message references a reputation
      problem we would have invented.
    """
    if not has_whatsapp:
        return NO_WHATSAPP
    if review_count is None or review_count < VOLUME_THRESHOLD:
        return EARLY_STAGE
    if rating is not None and rating < RATING_THRESHOLD:
        return REPUTATION
    if dine_in:
        return CAFE_SITE if site else CAFE_NOSITE
    return DELIVERY_SITE if site else DELIVERY_NOSITE


@dataclass(frozen=True, slots=True)
class Assignment:
    """One business's batch, and the derived fields that put it there."""

    # A slug from ``SLUGS``, or ``UNBATCHED``. Always a token, never ``None``:
    # every row has an answer, and for most of the database that answer is "the
    # cascade does not cover this category yet".
    batch: str
    wa_number: str | None
    wa_confidence: str | None  # §9.3's label

    @property
    def spec(self) -> Batch | None:
        """The batch definition, or ``None`` for ``UNBATCHED``."""
        return BY_SLUG.get(self.batch)

    @property
    def sendable(self) -> bool:
        return self.spec.sendable if self.spec else False


def assign(business: Any, contacts: Sequence[BatchContact]) -> Assignment:
    """§6's reference implementation, over a business and its *visible* contacts.

    ``contacts`` must be the set the table is showing — §15 suppression already
    applied. A business whose only WhatsApp number was suppressed is a business
    with no way to message it, and it belongs in `no-whatsapp` with the rest of
    them; reading the unsuppressed set here would put it in a send batch and
    hand the operator a number §15 says never to ring.

    A business outside ``BATCHED_CATEGORIES`` short-circuits to ``UNBATCHED``
    **before** the cascade runs — not after, and not into a fallback batch. The
    derived §2 fields are still computed, because "which number would I message
    this on" is a question about a phone and not about a vertical; only the
    routing is food-specific.
    """
    pick = pick_whatsapp(contacts)
    wa_number = pick.number if pick else None
    wa_confidence = pick.label if pick else None

    if not applies_to(getattr(business, "category", None)):
        return Assignment(
            batch=UNBATCHED, wa_number=wa_number, wa_confidence=wa_confidence
        )

    rating = business.rating
    return Assignment(
        batch=cascade(
            has_whatsapp=pick is not None,
            review_count=business.review_count,
            rating=float(rating) if rating is not None else None,
            dine_in=is_dine_in(business.subcategory),
            site=has_site(business.website),
        ),
        wa_number=wa_number,
        wa_confidence=wa_confidence,
    )

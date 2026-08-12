"""Extraction — hand the top *N* businesses of the current view to the dialler.

The operator's loop is not "export a CSV of 429 businesses". It is "give me the
next 30 numbers worth messaging, and do not give me the same 30 tomorrow". That
is two things this codebase did not have: a §9.3-filtered pull off the *top* of
the ranked table, and a memory of what has already been pulled.

Four rules, each with a reason:

* **The pull is the table.** ``extract`` takes the same ``ResultQuery`` the
  screen is showing and calls the same ``fetch_results``. Whatever the operator
  filtered to — one run, has-a-website, WhatsApp confirmed — is what gets
  extracted, in the order the table is sorted. This is §12.2's "respect the
  active filters" rule, applied to a second output.
* **Only ``confirmed`` and ``likely``, and only the label.** §9.3's raw evidence
  score stays internal; the operator asked for numbers the public record says
  take WhatsApp, and `no` is exactly the set that record argues against. Nothing
  here reaches the network to check — §9.3's standing rule.
* **Every number the business has, not §12.1's four.** The export caps at 4
  phone slots by rank; that is a *column-set* constraint and §10.1 forbids
  letting it become a data one. Extraction reads the ranked phone set, which is
  all of them.
* **A row that yields nothing still counts and is still marked.** The batch size
  is a count of *businesses worked*, not of numbers found, so "top 30" walks 30
  rows down the table. A business with no qualifying number was looked at and
  found wanting, and offering it again on the next pull would be the same dead
  row for ever. The count comes back in ``without_numbers`` so the screen can
  say so rather than the operator inferring it from a short clipboard.
* **``all`` is a size, and it is spelled out.** Working one outreach batch means
  draining it — "give me the 51 in `delivery-nosite` that I have not sent to"
  is one action, not two rounds of top-30 and a third that returns 11. So the
  limit is ``30 | 50 | 100 | "all"``, and ``"all"`` is a literal the caller has
  to type: a *missing* limit defaulting to everything is one dropped field
  between "pull a batch" and "empty the queue".

Which outreach batch each business was in (_BATCH_SPEC.md) is **recorded on the
ledger row**, next to the numbers and for the same reason: it is a record of what
went out. A business re-scored since, or one whose site was found on a later run,
moves batch — and the answer to "what did I send the `cafe-nosite` message to?"
must not move with it.

Marking is *not* suppression. §15's ``do_not_contact`` says "never contact
this"; the ledger says "already sent". Clearing an entry puts the business back
in the queue, which is the whole point, and no ``do_not_contact`` row is written
or read here.

**Two simultaneous pulls are not handled, deliberately.** Both would select the
same top *N*, and the second would fail on ``extractions``' unique index — which
rolls its whole batch back rather than half-marking anything, so the failure mode
is loud and safe. §13 describes a single operator and the button disables while a
pull is in flight; a reservation protocol here would be machinery for a race that
one person clicking one button cannot have.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from leadscraper.core import batches
from leadscraper.db.models import Business, Contact, Extraction
from leadscraper.enums import ContactKind
from leadscraper.logging import get_logger
from leadscraper.services.results import (
    ResultEntry,
    ResultQuery,
    Suppression,
    fetch_results,
    load_suppression,
)

log = get_logger(__name__)

# §9.3's labels worth ringing. `no` is not "unknown" — it is the record arguing
# against the number — so it is the one label a WhatsApp pull must exclude.
#
# **The same object the batch cascade uses**, not a copy of it: _BATCH_SPEC's
# `no-whatsapp` batch is defined as "the pull would yield nothing here", so two
# frozensets that drifted apart would put businesses in a send batch that the
# clipboard then skips.
EXTRACTABLE_LABELS: frozenset[str] = batches.WA_VALID_LABELS

# The three sizes §13's Extract control offers, plus "all". An allow-list rather
# than a free integer: this endpoint writes rows, and "top 100000" is a mis-click
# that empties the queue in one go.
BATCH_SIZES: tuple[int, ...] = (30, 50, 100)

# Drain the filtered view. Spelled as a literal so it cannot be arrived at by
# omission — see the module docstring.
EXTRACT_ALL: Literal["all"] = "all"
type ExtractLimit = int | Literal["all"]

# One number per line — a dialler, a spreadsheet column and every bulk-messaging
# tool the operator might paste into all read that; a comma-joined line reads as
# one field in two of the three.
CLIPBOARD_SEPARATOR = "\n"


class UnsupportedBatchSize(ValueError):
    """A size outside ``BATCH_SIZES``. Surfaced as a 422, not clamped."""


@dataclass(slots=True)
class ExtractedBusiness:
    """One business in a pull, as the operator needs to see it."""

    business_id: uuid.UUID
    run_id: uuid.UUID
    name: str
    city: str | None
    lead_score: int | None
    website: str | None
    numbers: list[str]
    # _BATCH_SPEC's cascade, as it stood at pull time — which message this
    # business is getting.
    batch: str | None = None


@dataclass(slots=True)
class ExtractionResult:
    numbers: list[str] = field(default_factory=list)
    businesses: list[ExtractedBusiness] = field(default_factory=list)
    batch_id: uuid.UUID | None = None
    # ``None`` is "all" — the pull was asked to drain the view rather than to
    # take a fixed count off the top of it.
    requested: int | None = 0
    # Businesses newly written to the ledger. Equals ``len(businesses)``; named
    # separately because the two would diverge the day this grows a dry-run.
    marked: int = 0
    # Rows passed over because they were already on the ledger.
    skipped_already_extracted: int = 0
    # Rows in the batch that had no `confirmed`/`likely` number at all.
    without_numbers: int = 0
    # Rows left in the filtered view after this pull.
    remaining: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def clipboard(self) -> str:
        return CLIPBOARD_SEPARATOR.join(self.numbers)


def extractable_numbers(contacts: Iterable[Contact]) -> list[str]:
    """A business's messageable numbers, in §3.3 rank order.

    ``rank is None`` is excluded for the same reason §12.1 excludes it: it means
    either that ``whatsapp_only`` filtered the number out or that a §10.1 merge
    left a second provenance row on a number that already holds its place. The
    first is a deliberate exclusion; the second would put the same number on the
    clipboard twice.
    """
    phones = [
        c
        for c in contacts
        if c.kind == ContactKind.PHONE
        and c.rank is not None
        and c.wa_label in EXTRACTABLE_LABELS
    ]
    phones.sort(key=lambda c: (c.rank or 0, c.value_e164 or ""))

    seen: dict[str, None] = {}
    for contact in phones:
        value = contact.value_e164 or contact.value_raw
        if value:
            seen.setdefault(value, None)
    return list(seen)


def extract(
    session: Session,
    query: ResultQuery,
    limit: ExtractLimit,
    *,
    now: datetime | None = None,
) -> ExtractionResult:
    """Pull un-extracted businesses off the top of ``query``.

    ``limit`` is one of ``BATCH_SIZES`` or ``EXTRACT_ALL``. "All" is still a pull
    off the top of the same query — it takes every un-extracted row *in this
    filtered view*, which is why it is safe to point at one outreach batch and
    why it is not a "extract everything in the database" button.
    """
    take_all = limit == EXTRACT_ALL
    if not take_all and limit not in BATCH_SIZES:
        raise UnsupportedBatchSize(
            f"Batch size must be one of {', '.join(map(str, BATCH_SIZES))} "
            f'or "{EXTRACT_ALL}"; got {limit!r}.'
        )

    # The whole filtered set in sort order, not the page the operator scrolled
    # to. "Top 30" is a claim about the table, and a paginated read would make it
    # a claim about the viewport instead.
    page = fetch_results(session, replace(query, limit=None, offset=0))

    result = ExtractionResult(
        requested=None if take_all else limit, batch_id=uuid.uuid4()
    )
    chosen: list[ResultEntry] = []
    for entry in page.entries:
        if entry.row.get("_extracted"):
            result.skipped_already_extracted += 1
            continue
        if take_all or len(chosen) < limit:
            chosen.append(entry)
        else:
            result.remaining += 1

    stamp = now or datetime.now(UTC)
    seen: dict[str, None] = {}

    for entry in chosen:
        numbers = extractable_numbers(entry.contacts)
        if not numbers:
            result.without_numbers += 1
        for value in numbers:
            seen.setdefault(value, None)

        business = entry.business
        # Read off the row rather than recomputed: the row is what the table,
        # the CSV and this pull all agree on, and `_batch` there was assigned
        # against the §15-suppressed contact set.
        batch = entry.row.get("_batch")
        result.businesses.append(
            ExtractedBusiness(
                business_id=business.id,
                run_id=business.run_id,
                name=business.name,
                city=business.city,
                lead_score=business.lead_score,
                website=business.website,
                numbers=numbers,
                batch=batch,
            )
        )
        session.add(
            Extraction(
                business_id=business.id,
                run_id=business.run_id,
                batch_id=result.batch_id,
                # NULL for an "all" pull. "top 100" and "everything that was
                # left" are different facts about how a row was chosen, and
                # writing the realised count would record the second as the
                # first.
                batch_size=None if take_all else limit,
                batch=batch,
                numbers=numbers,
                extracted_at=stamp,
            )
        )
        result.marked += 1

    # Across the batch, not per business: the same landline serves two branches
    # often enough that a clipboard of 30 businesses is routinely fewer than 30
    # distinct numbers, and the operator should not message it twice.
    result.numbers = list(seen)

    session.commit()

    if result.marked == 0 or (not take_all and result.marked < limit):
        asked = "every un-extracted row" if take_all else f"{limit} businesses"
        result.warnings.append(
            f"Asked for {asked}, marked {result.marked}. The filtered view had "
            "no more un-extracted rows — widen the filters, or clear entries "
            "from the extracted list to make them eligible again."
        )
    if result.without_numbers:
        result.warnings.append(
            f"{result.without_numbers} of {result.marked} businesses had no "
            "confirmed or likely WhatsApp number and contributed nothing to the "
            "clipboard. They are marked extracted so the next pull moves past "
            "them rather than offering the same dead rows again."
        )

    log.info(
        "extraction.pulled",
        batch_id=str(result.batch_id),
        requested=limit,
        batches=sorted({b.batch for b in result.businesses if b.batch}),
        marked=result.marked,
        numbers=len(result.numbers),
        skipped=result.skipped_already_extracted,
        without_numbers=result.without_numbers,
    )
    return result


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #


def list_extractions(
    session: Session,
    run_ids: Sequence[uuid.UUID] = (),
    limit: int | None = None,
    *,
    batch_tokens: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """The extracted list, newest first, optionally one outreach batch of it.

    Joins the business back in so the screen can show a name rather than a UUID.
    A business deleted since (§15's bulk delete) takes its ledger row with it —
    the row was a pointer at a business, and there is no honest way to render one
    whose business is gone.

    **Two batch fields, because they answer different questions.** ``batch`` is
    what was recorded when the pull happened: "which message did this business
    get?", and nothing may rewrite it. ``current_batch`` is the cascade run
    against the business as it stands now: "which message would it get today?".
    They diverge whenever a later run finds a website or a review count moves a
    business over 200, and that divergence is worth seeing rather than
    resolving — it is the case where a follow-up should not repeat the first
    message.

    Rows written before the ``batch`` column existed have ``batch = None``. That
    stays ``None``: recomputing it would be asserting a fact about a past pull
    from present data. The filter falls back to ``current_batch`` for those, so
    the screen is still useful on the ledger that already exists, and the UI says
    which of the two it is showing. ``None`` and ``"unbatched"`` are therefore
    different answers and are stored differently: the first is "we did not record
    one", the second is "the cascade had no definition covering this business" —
    which is every category except ``food``.
    """
    statement = (
        select(Extraction)
        .options(
            selectinload(Extraction.business).selectinload(Business.contacts),
            selectinload(Extraction.run),
        )
        .order_by(Extraction.extracted_at.desc(), Extraction.created_at.desc())
    )
    if run_ids:
        statement = statement.where(Extraction.run_id.in_(run_ids))

    wanted = {token for token in map(batches.resolve_token, batch_tokens) if token}
    # ``current_batch`` needs the same §15-suppressed contact set the table
    # assigns batches against, or the ledger would file a business under a batch
    # the results screen does not show it in.
    suppression = load_suppression(session)

    out: list[dict[str, Any]] = []
    for entry in session.execute(statement).scalars():
        business: Business | None = entry.business
        current = _current_batch(business, suppression)
        if batch_tokens and (entry.batch or current) not in wanted:
            continue
        out.append(
            {
                "id": entry.id,
                "business_id": entry.business_id,
                "run_id": entry.run_id,
                "batch_id": entry.batch_id,
                "batch_size": entry.batch_size,
                "batch": entry.batch,
                "current_batch": current,
                # Stored at pull time, not recomputed: this is a record of what
                # went out, and a business enriched since would otherwise
                # rewrite history.
                "numbers": list(entry.numbers or ()),
                "extracted_at": entry.extracted_at or entry.created_at,
                "business_name": business.name if business else None,
                "city": business.city if business else None,
                "category": business.category if business else None,
                "website": business.website if business else None,
                "lead_score": business.lead_score if business else None,
                "run_city": entry.run.city if entry.run else None,
                "run_category": entry.run.category if entry.run else None,
            }
        )
        # Applied after the batch filter, never in SQL: a LIMIT before the filter
        # would return "the newest 50 rows, of which some are in this batch"
        # rather than "the newest 50 rows in this batch".
        if limit and len(out) >= limit:
            break
    return out


def _current_batch(business: Business | None, suppression: Suppression) -> str | None:
    if business is None:
        return None
    visible = [c for c in business.contacts if not suppression.blocks_contact(c)]
    return batches.assign(business, visible).batch


def batch_counts(
    session: Session,
    run_ids: Sequence[uuid.UUID] = (),
) -> dict[str, int]:
    """How many ledger rows sit in each batch — the chips above the list.

    Counted on the same ``batch or current_batch`` the filter uses, so a count
    and the list it labels can never disagree.
    """
    counts = dict.fromkeys(batches.FILTER_TOKENS, 0)
    for row in list_extractions(session, run_ids):
        slug = row["batch"] or row["current_batch"]
        if slug in counts:
            counts[slug] += 1
    return counts


def clear_extraction(session: Session, extraction_id: uuid.UUID) -> bool:
    """Take one business off the ledger. It becomes extractable again."""
    entry = session.get(Extraction, extraction_id)
    if entry is None:
        return False
    session.delete(entry)
    session.commit()
    log.info("extraction.cleared", extraction_id=str(extraction_id))
    return True


def clear_extractions(
    session: Session,
    run_ids: Sequence[uuid.UUID] = (),
    *,
    batch_tokens: Sequence[str] = (),
) -> int:
    """Empty the ledger, optionally for one run and one batch only.

    Deletes nothing but ledger rows — no business, no contact, no
    ``do_not_contact`` entry. "I have not sent to these after all" is the only
    claim being retracted.

    The batch scope exists so that **what a Clear button deletes is what the
    screen was showing**. The list can be filtered to one batch; a clear that
    ignored that filter would delete the run's whole ledger from a screen
    displaying 22 rows of it, and the operator would find out by re-extracting
    130 businesses they had already messaged.

    Batch-scoped deletes go through ``list_extractions`` rather than a WHERE on
    the column, because the filter it implements is ``batch or current_batch``
    and pre-column rows have no ``batch`` to match on — a SQL predicate would
    quietly leave exactly those rows behind.
    """
    if batch_tokens:
        ids = [
            row["id"]
            for row in list_extractions(session, run_ids, batch_tokens=batch_tokens)
        ]
        if not ids:
            return 0
        statement = delete(Extraction).where(Extraction.id.in_(ids))
    else:
        statement = delete(Extraction)
        if run_ids:
            statement = statement.where(Extraction.run_id.in_(run_ids))

    deleted = session.execute(statement).rowcount or 0
    session.commit()
    log.info(
        "extraction.cleared_all",
        deleted=deleted,
        runs=len(run_ids),
        batches=list(batch_tokens),
    )
    return deleted

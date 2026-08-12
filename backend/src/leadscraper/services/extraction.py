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
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from leadscraper.db.models import Business, Contact, Extraction
from leadscraper.enums import ContactKind, WhatsAppLabel
from leadscraper.logging import get_logger
from leadscraper.services.results import ResultEntry, ResultQuery, fetch_results

log = get_logger(__name__)

# §9.3's labels worth ringing. `no` is not "unknown" — it is the record arguing
# against the number — so it is the one label a WhatsApp pull must exclude.
EXTRACTABLE_LABELS: frozenset[str] = frozenset(
    {WhatsAppLabel.CONFIRMED, WhatsAppLabel.LIKELY}
)

# The three sizes §13's Extract control offers. An allow-list rather than a free
# integer: this endpoint writes rows, and "top 100000" is a mis-click that
# empties the queue in one go.
BATCH_SIZES: tuple[int, ...] = (30, 50, 100)

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


@dataclass(slots=True)
class ExtractionResult:
    numbers: list[str] = field(default_factory=list)
    businesses: list[ExtractedBusiness] = field(default_factory=list)
    batch_id: uuid.UUID | None = None
    requested: int = 0
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
    limit: int,
    *,
    now: datetime | None = None,
) -> ExtractionResult:
    """Pull the next ``limit`` un-extracted businesses off the top of ``query``."""
    if limit not in BATCH_SIZES:
        raise UnsupportedBatchSize(
            f"Batch size must be one of {', '.join(map(str, BATCH_SIZES))}; got {limit}."
        )

    # The whole filtered set in sort order, not the page the operator scrolled
    # to. "Top 30" is a claim about the table, and a paginated read would make it
    # a claim about the viewport instead.
    page = fetch_results(session, replace(query, limit=None, offset=0))

    result = ExtractionResult(requested=limit, batch_id=uuid.uuid4())
    chosen: list[ResultEntry] = []
    for entry in page.entries:
        if entry.row.get("_extracted"):
            result.skipped_already_extracted += 1
            continue
        if len(chosen) < limit:
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
        result.businesses.append(
            ExtractedBusiness(
                business_id=business.id,
                run_id=business.run_id,
                name=business.name,
                city=business.city,
                lead_score=business.lead_score,
                website=business.website,
                numbers=numbers,
            )
        )
        session.add(
            Extraction(
                business_id=business.id,
                run_id=business.run_id,
                batch_id=result.batch_id,
                batch_size=limit,
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

    if result.marked < limit:
        result.warnings.append(
            f"Asked for {limit} businesses, marked {result.marked}. The filtered "
            "view had no more un-extracted rows — widen the filters, or clear "
            "entries from the extracted list to make them eligible again."
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
) -> list[dict[str, Any]]:
    """The extracted list, newest first.

    Joins the business back in so the screen can show a name rather than a UUID.
    A business deleted since (§15's bulk delete) takes its ledger row with it —
    the row was a pointer at a business, and there is no honest way to render one
    whose business is gone.
    """
    statement = (
        select(Extraction)
        .options(selectinload(Extraction.business), selectinload(Extraction.run))
        .order_by(Extraction.extracted_at.desc(), Extraction.created_at.desc())
    )
    if run_ids:
        statement = statement.where(Extraction.run_id.in_(run_ids))
    if limit:
        statement = statement.limit(limit)

    out: list[dict[str, Any]] = []
    for entry in session.execute(statement).scalars():
        business: Business | None = entry.business
        out.append(
            {
                "id": entry.id,
                "business_id": entry.business_id,
                "run_id": entry.run_id,
                "batch_id": entry.batch_id,
                "batch_size": entry.batch_size,
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
    return out


def clear_extraction(session: Session, extraction_id: uuid.UUID) -> bool:
    """Take one business off the ledger. It becomes extractable again."""
    entry = session.get(Extraction, extraction_id)
    if entry is None:
        return False
    session.delete(entry)
    session.commit()
    log.info("extraction.cleared", extraction_id=str(extraction_id))
    return True


def clear_extractions(session: Session, run_ids: Sequence[uuid.UUID] = ()) -> int:
    """Empty the ledger, optionally for one run only.

    Deletes nothing but ledger rows — no business, no contact, no
    ``do_not_contact`` entry. "I have not sent to these after all" is the only
    claim being retracted.
    """
    statement = delete(Extraction)
    if run_ids:
        statement = statement.where(Extraction.run_id.in_(run_ids))
    deleted = session.execute(statement).rowcount or 0
    session.commit()
    log.info("extraction.cleared_all", deleted=deleted, runs=len(run_ids))
    return deleted

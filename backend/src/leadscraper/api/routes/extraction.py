"""Extraction — the pull, and the ledger of what has been pulled.

``POST /api/extractions`` takes its **filters from the query string**, not from
the body, and reads them through the same ``result_query`` dependency the table
and the CSV use. So the three outputs of §13 Screen 3 — what is on screen, what
the CSV contains, what the clipboard gets — are one query with three
renderings, and the operator's "extract whatever filter is applied" is true by
construction rather than by two call sites being kept in step.

The rest of the module is the ledger: list it, clear one row, clear the lot.
Clearing puts a business back in the queue and does nothing else — it is not
§15, and §15's ``do_not_contact`` is not touched from here in either direction.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from leadscraper.api.deps import QueryDep, SessionDep
from leadscraper.api.schemas import (
    ExtractedBusinessOut,
    ExtractionCleared,
    ExtractionEntry,
    ExtractionRequest,
    ExtractionResponse,
)
from leadscraper.services.extraction import (
    UnsupportedBatchSize,
    clear_extraction,
    clear_extractions,
    extract,
    list_extractions,
)

router = APIRouter(prefix="/api/extractions", tags=["extraction"])


@router.post("", response_model=ExtractionResponse)
def create_extraction(
    payload: ExtractionRequest, session: SessionDep, query: QueryDep
) -> ExtractionResponse:
    """Pull the top *N* un-extracted businesses of the current filtered view."""
    try:
        result = extract(session, query, payload.limit)
    except UnsupportedBatchSize as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    return ExtractionResponse(
        batch_id=result.batch_id,
        clipboard=result.clipboard,
        numbers=result.numbers,
        businesses=[ExtractedBusinessOut(**asdict(b)) for b in result.businesses],
        requested=result.requested,
        marked=result.marked,
        skipped_already_extracted=result.skipped_already_extracted,
        without_numbers=result.without_numbers,
        remaining=result.remaining,
        warnings=result.warnings,
    )


@router.get("", response_model=list[ExtractionEntry])
def get_extractions(
    session: SessionDep,
    run: Annotated[list[uuid.UUID] | None, Query(description="repeatable")] = None,
    limit: Annotated[int | None, Query(ge=1, le=5_000)] = None,
) -> list[dict]:
    return list_extractions(session, run or (), limit)


@router.delete("", response_model=ExtractionCleared)
def delete_extractions(
    session: SessionDep,
    run: Annotated[list[uuid.UUID] | None, Query(description="repeatable")] = None,
) -> ExtractionCleared:
    """Clear the whole ledger, or one run's worth of it.

    Nothing but ledger rows is deleted. The businesses, their contacts and the
    §15 suppression list are all untouched — the only claim being retracted is
    "these have already been sent".
    """
    return ExtractionCleared(cleared=clear_extractions(session, run or ()))


@router.delete("/{extraction_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_extraction(extraction_id: uuid.UUID, session: SessionDep) -> None:
    """Take one business off the ledger; the next pull can offer it again."""
    if not clear_extraction(session, extraction_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such extraction entry.")

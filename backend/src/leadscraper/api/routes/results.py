"""§13 Screen 3's table and §12.2's CSV — one query layer, two renderings.

Both endpoints take the same ``result_query`` dependency and call the same
``fetch_results``. §12.2 says the export must "respect the active table filters
and sort order", so the only difference between these two handlers is whether
the rows are serialised as JSON or written through ``export/csv_writer``.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from leadscraper.api.deps import QueryDep, SessionDep
from leadscraper.api.schemas import ResultsResponse
from leadscraper.db.models import Run
from leadscraper.export import COLUMNS, COMPACT_COLUMNS, export_filename
from leadscraper.export.columns import NUMERIC_COLUMNS
from leadscraper.export.csv_writer import iter_csv
from leadscraper.services.results import ResultQuery, fetch_results

router = APIRouter(prefix="/api", tags=["results"])


@router.get("/results", response_model=ResultsResponse)
def get_results(session: SessionDep, query: QueryDep) -> ResultsResponse:
    """The §12.1 projection, filtered and sorted as §13 Screen 3 asked."""
    page = fetch_results(session, query)
    return ResultsResponse(
        rows=page.rows,
        total=page.total,
        columns=list(COLUMNS),
        compact_columns=list(COMPACT_COLUMNS),
        numeric_columns=sorted(NUMERIC_COLUMNS),
        suppressed_contacts=page.suppressed_contacts,
        suppressed_businesses=page.suppressed_businesses,
        collapsed=page.collapsed,
        cities=list(page.cities),
        categories=list(page.categories),
        batch_counts=page.batch_counts,
    )


@router.get("/runs/{run_id}/export.csv")
def export_run_csv(run_id: uuid.UUID, session: SessionDep, query: QueryDep):
    """§12.2's endpoint, at the path §12.2 specifies.

    Scoped to one run whatever ``run_id`` query params also arrived, so the URL
    in the address bar means what it says.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such run: {run_id}")

    # The export is the whole filtered set, not the page the operator has
    # scrolled to — §12.2's filename carries the row count, and a file called
    # `…_50leads.csv` holding one page of a 500-row result would be a lie.
    scoped = replace(query, run_ids=(run_id,), limit=None, offset=0)
    return _csv_response(session, scoped, run.city, run.category)


@router.get("/results/export.csv")
def export_results_csv(session: SessionDep, query: QueryDep):
    """The same export across a selection of runs (§10.1's read-side union).

    City and category are taken from the result set rather than assumed: a
    cross-run export can span both, and §12.2's filename must not claim
    otherwise.
    """
    return _csv_response(session, replace(query, limit=None, offset=0), None, None)


def _csv_response(
    session,
    query: ResultQuery,
    city: str | None,
    category: str | None,
) -> StreamingResponse:
    """Materialise the filtered set, then stream it.

    §12.2's filename carries the row count, so the rows have to be counted before
    the first byte goes out — there is no streaming the header of a file whose
    name depends on the last row. The largest run is 199 businesses; this is a
    page of the table, not a bulk dump.
    """
    page = fetch_results(session, query)

    # Only when the export really is one city / one category. §12.2's filename
    # is a claim about the file's contents.
    resolved_city = city if city is not None else _single(page.cities)
    resolved_category = category if category is not None else _single(page.categories)

    filename = export_filename(
        resolved_city, resolved_category, page.total, now=datetime.now()
    )

    return StreamingResponse(
        iter_csv(page.rows),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The suppression counts ride along so the operator can reconcile a
            # file with the screen it came from — §15 applied silently is §15
            # nobody trusts.
            "X-Leads-Total": str(page.total),
            "X-Leads-Suppressed-Contacts": str(page.suppressed_contacts),
            "X-Leads-Suppressed-Businesses": str(page.suppressed_businesses),
            "X-Leads-Collapsed": str(page.collapsed),
        },
    )


def _single(values: tuple[str, ...]) -> str | None:
    return values[0] if len(values) == 1 else None

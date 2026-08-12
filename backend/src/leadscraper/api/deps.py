"""Shared dependencies — the DB session, and the §13 filter bar.

``result_query`` is the load-bearing one. §12.2 requires the CSV to "respect the
active table filters and sort order", so the table endpoint and the export
endpoint take the **same dependency** and build the same ``ResultQuery`` from the
same query string. Parsing the filters twice is how they would come to disagree.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from leadscraper.db.session import get_session
from leadscraper.services.results import DEFAULT_SORT, SORTABLE, ResultQuery

# §13 Screen 3 virtualises rows, so the page size is about keeping one response
# sane rather than about what fits on screen. The largest run is 199 businesses.
MAX_PAGE = 5_000


def db_session() -> Iterator[Session]:
    session = get_session()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(db_session)]


def _split(value: str | None) -> tuple[str, ...]:
    """Comma-separated query params, which is what a URL bar and TanStack agree on."""
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def result_query(
    # Named `run`, not `run_id`: this dependency is also mounted on
    # `/runs/{run_id}/export.csv`, and FastAPI resolves a dependency parameter
    # sharing a path parameter's name as that path parameter.
    run: Annotated[list[uuid.UUID] | None, Query(description="repeatable")] = None,
    whatsapp: Annotated[str | None, Query(description="confirmed,likely,no")] = None,
    has_owner_name: Annotated[bool | None, Query()] = None,
    has_website: Annotated[
        bool | None,
        Query(description="true = a website is on record, false = none is, omitted = any"),
    ] = None,
    min_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    line_type: Annotated[str | None, Query(description="mobile,landline,uan")] = None,
    source: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(description="free text; matches phone digits too")] = None,
    sort: Annotated[str, Query()] = DEFAULT_SORT,
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
    collapse: Annotated[bool, Query(description="§10.1 read-side union on place_id")] = False,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ResultQuery:
    return ResultQuery(
        run_ids=tuple(run or ()),
        whatsapp=_split(whatsapp),
        has_owner_name=has_owner_name,
        has_website=has_website,
        min_score=min_score,
        line_types=_split(line_type),
        sources=_split(source),
        search=q,
        # An unknown sort column falls back to `lead_score` rather than 400ing.
        # The sort key arrives from a clicked header, and a broken column name
        # should give the operator the default table, not an error page.
        sort=sort if sort in SORTABLE else DEFAULT_SORT,
        descending=order == "desc",
        collapse_place_id=collapse,
        limit=limit,
        offset=offset,
    )


QueryDep = Annotated[ResultQuery, Depends(result_query)]

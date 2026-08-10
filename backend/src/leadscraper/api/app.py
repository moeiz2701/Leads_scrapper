"""The FastAPI application (§2's "API / orchestration" layer).

``fastapi`` and ``uvicorn`` have been dependencies since Phase 1 and nothing
imported them; this is the app they were for.

    cd backend
    uv run uvicorn leadscraper.api.app:app --reload --port 8000

Route handlers are ``def``, not ``async def``, so Starlette runs them in its
threadpool — the ORM here is deliberately synchronous (see ``db/session``), and
a sync session inside an async handler would block the event loop for every
other request.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from leadscraper.api.routes import meta, results, runs, suppression
from leadscraper.config import get_settings
from leadscraper.logging import configure_logging, get_logger
from leadscraper.pipeline.queues import queue_health
from leadscraper.pipeline.stages import StageNotImplementedError

log = get_logger(__name__)

# The Next.js dev server. A single-operator tool on localhost, so this is the
# whole origin list rather than a configurable one.
DEV_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    health = queue_health()
    if not health.get("redis"):
        log.error("api.redis_unavailable", detail=health.get("error"))
    elif not health.get("workers") and not get_settings().queue_sync:
        # Worth a loud line at boot: with no worker and no sync mode a created
        # run sits at `queued` forever, which looks like a hang rather than a
        # missing process. §5.5's rule, applied to our own plumbing.
        log.warning(
            "api.no_workers",
            hint="start one with `uv run python scripts/worker.py`, "
            "or set QUEUE_SYNC=true to run stages inline",
        )
    yield


app = FastAPI(
    title="Pakistan Local Business Lead Scraper",
    version="0.5.0",
    summary="§13's three screens, over the §2 pipeline.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=DEV_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # §12.2's export carries its filename and the §15 suppression counts in
    # headers; without this the browser cannot read either.
    expose_headers=[
        "Content-Disposition",
        "X-Leads-Total",
        "X-Leads-Suppressed-Contacts",
        "X-Leads-Suppressed-Businesses",
        "X-Leads-Collapsed",
    ],
)

app.include_router(runs.router)
app.include_router(results.router)
app.include_router(suppression.router)
app.include_router(meta.router)


@app.exception_handler(StageNotImplementedError)
def _stage_not_implemented(request: Request, exc: StageNotImplementedError):
    """A stage without a body is a schedule, not a server error.

    501 rather than 500, and the phase is named — the operator needs to know
    whether to retry or to wait for a release.
    """
    return JSONResponse(
        status_code=501,
        content={
            "error": "stage_not_implemented",
            "stage": exc.stage.value,
            "phase": exc.phase,
            "message": str(exc),
        },
    )


@app.get("/api/health", tags=["meta"])
def health() -> dict:
    """Is anything going to consume a run if one is created?"""
    return {"status": "ok", "queue": queue_health()}

"""Request and response shapes for the §13 screens.

Kept separate from the ORM on purpose: §12.1's column set is the contract the
frontend codes against, and a table row is a *projection* (``export/rows.py``)
rather than a serialised ``Business``. Letting the ORM leak into the API would
tie the screen to the schema and make §11 changes breaking changes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from leadscraper.enums import Category, NumberPreference


class SourceFlags(BaseModel):
    """§13 Screen 1's source toggles. Core sources default on, §6 defaults off."""

    google_maps: bool = True
    business_website: bool = True
    # §3 lists directories as "core, default on". Flipped off on the Phase 6
    # measurement, and recorded as a departure in the README: across four slices
    # and 333 harvested listings the §5.3 layer added **0 contacts and
    # corroborated 0 businesses**. A source that is on by default and always
    # contributes zero is §5.5's failure mode wearing a toggle, so the operator
    # now opts in. The module is built, tested and correct — see §5.3's Phase 6
    # note for why it still yields nothing.
    directories: bool = False
    facebook: bool = False
    instagram: bool = False


class RunCreate(BaseModel):
    city: str
    category: Category
    subcategories: list[str] = Field(default_factory=list)
    number_preference: NumberPreference = NumberPreference.OWNER_FIRST
    sources: SourceFlags = Field(default_factory=SourceFlags)
    target_leads: int | None = Field(default=None, ge=1, le=100_000)
    # §14's recommendation after Phase 2's measurements: "tune the plan down, not
    # up — 3–4 synonyms × 6–8 tiles is likely enough". Exposed so the operator can
    # act on it, and defaulted to the full plan so nothing changes silently.
    synonym_limit: int | None = Field(default=None, ge=1, le=20)
    tile_limit: int | None = Field(default=None, ge=1, le=40)

    @field_validator("subcategories")
    @classmethod
    def _strip(cls, value: list[str]) -> list[str]:
        return [v.strip() for v in value if v.strip()]


class RunSummary(BaseModel):
    id: uuid.UUID
    city: str | None
    category: str | None
    number_preference: str
    status: str
    mode: str
    businesses: int = 0
    qualified: int = 0
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class StageProgress(BaseModel):
    """One row of §13 Screen 2's per-stage counters."""

    stage: str
    state: str  # pending | running | done | unimplemented | failed
    processed: int | None = None
    produced: int | None = None
    elapsed_seconds: float | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None


class SourcePill(BaseModel):
    """§13 Screen 2's per-source status pill."""

    source: str
    status: str
    detail: str | None = None
    updated_at: datetime | None = None


class RunDetail(RunSummary):
    subcategories: list[str] = Field(default_factory=list)
    sources_enabled: dict[str, Any] = Field(default_factory=dict)
    stats: dict[str, Any] = Field(default_factory=dict)
    stages: list[StageProgress] = Field(default_factory=list)
    sources: list[SourcePill] = Field(default_factory=list)
    queue_depths: dict[str, int] = Field(default_factory=dict)
    # The §10.2 ceiling, restated on every run. §8's engine is Phase 9, so the
    # 15-point person term is 0 on all but one business in 199 — a UI that draws
    # a bar out of 100 would otherwise imply the missing 15 points are a fact
    # about the business rather than about the pipeline.
    unattributed_ceiling: int | None = None


class RunCreated(BaseModel):
    run: RunSummary
    stages_planned: list[str]
    stages_unavailable: list[str] = Field(default_factory=list)
    job_id: str | None = None
    warnings: list[str] = Field(default_factory=list)


class EstimateRequest(BaseModel):
    city: str
    category: Category
    synonym_limit: int | None = Field(default=None, ge=1, le=20)
    tile_limit: int | None = Field(default=None, ge=1, le=40)
    enrich: bool = True
    # §6 Stage 3, off by default like the toggles themselves (§6.6). It is a
    # separate flag rather than folded into ``enrich`` because it dominates the
    # runtime when on — the social pass on Lahore × food is ~45 minutes against
    # ~12 for discovery — and Screen 1 must not quote a number that omits it.
    social: bool = False


class BatchInfo(BaseModel):
    """One outreach batch of _BATCH_SPEC.md §4, for the picker.

    Served rather than hard-coded in the frontend for the reason the city and
    synonym vocabularies are: a second copy of the cascade's vocabulary in
    TypeScript is the fastest way to have the UI name a batch the backend does
    not have.
    """

    id: str
    slug: str
    name: str
    definition: str
    send_priority: int | None = None
    sendable: bool = True
    note: str


class BatchCatalogue(BaseModel):
    """The batch vocabulary, and the honest limits of it."""

    batches: list[BatchInfo] = Field(default_factory=list)
    # The reserved non-batch token. Named rather than assumed, so the UI does
    # not carry the string "unbatched" as a literal.
    unbatched_slug: str
    # The categories the cascade has definitions for. One, today. The UI shows
    # this so a salon operator is told the layer does not cover them yet rather
    # than being shown seven empty batches and left to guess why.
    categories: list[str] = Field(default_factory=list)
    note: str


class ResultsResponse(BaseModel):
    rows: list[dict[str, Any]]
    total: int
    columns: list[str]
    compact_columns: list[str]
    numeric_columns: list[str]
    suppressed_contacts: int = 0
    suppressed_businesses: int = 0
    collapsed: int = 0
    cities: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    # Slug → size, for the whole view *before* the batch filter. Lets the picker
    # show what selecting a batch would cost without a second request per batch.
    batch_counts: dict[str, int] = Field(default_factory=dict)


class ExtractionRequest(BaseModel):
    """Extract → top N of the current view, or all of it.

    The *filters* are not in this body — they arrive as the query string, read by
    the same ``result_query`` dependency the table and the CSV use. That is the
    point: "extract what I am looking at" and "export what I am looking at" have
    to mean the same thing, and they cannot if one of them re-declares the
    filter set here. The outreach batch is a filter like any other, so it travels
    in the query string too.
    """

    # No default. "All" has to be asked for by name — a limit that could be
    # omitted into meaning "everything" is one dropped field away from draining
    # the queue.
    limit: int | Literal["all"] = Field(
        description='30, 50 or 100 — services.extraction.BATCH_SIZES — or "all"'
    )


class ExtractedBusinessOut(BaseModel):
    business_id: uuid.UUID
    run_id: uuid.UUID
    name: str
    city: str | None = None
    lead_score: int | None = None
    website: str | None = None
    numbers: list[str] = Field(default_factory=list)
    batch: str | None = None


class ExtractionResponse(BaseModel):
    """What one Extract click produced, and what it could not."""

    batch_id: uuid.UUID | None = None
    # The clipboard payload, built server-side so its format has one definition
    # and a test can pin it.
    clipboard: str = ""
    numbers: list[str] = Field(default_factory=list)
    businesses: list[ExtractedBusinessOut] = Field(default_factory=list)
    # ``None`` = the pull was asked to drain the view rather than take a count.
    requested: int | None = 0
    marked: int = 0
    skipped_already_extracted: int = 0
    without_numbers: int = 0
    remaining: int = 0
    warnings: list[str] = Field(default_factory=list)


class ExtractionEntry(BaseModel):
    """One row of the extracted list."""

    id: uuid.UUID
    business_id: uuid.UUID
    run_id: uuid.UUID
    batch_id: uuid.UUID
    # ``None`` = an "extract all" pull rather than a fixed top-N.
    batch_size: int | None = None
    # Recorded at pull time. ``None`` on rows written before the column existed —
    # not backfilled, because that would assert a fact about a past pull.
    batch: str | None = None
    # The cascade run against the business as it stands now. Diverges from
    # ``batch`` when a later run moved the business, which is worth seeing.
    current_batch: str | None = None
    numbers: list[str] = Field(default_factory=list)
    extracted_at: datetime | None = None
    business_name: str | None = None
    city: str | None = None
    category: str | None = None
    website: str | None = None
    lead_score: int | None = None
    run_city: str | None = None
    run_category: str | None = None


class ExtractionCleared(BaseModel):
    cleared: int


class SuppressionCreate(BaseModel):
    """§15 — a removal request. At least one identifier is required."""

    value_e164: str | None = None
    email: str | None = None
    domain: str | None = None
    reason: str | None = None
    added_by: str | None = None


class SuppressionEntry(SuppressionCreate):
    id: uuid.UUID
    created_at: datetime | None = None


class BulkDeleteRequest(BaseModel):
    """§13 Screen 3's "bulk select → delete", which §15 requires in v1."""

    business_ids: list[uuid.UUID] = Field(min_length=1)
    reason: str | None = None
    added_by: str | None = None
    # Deleting without suppressing is the one thing this endpoint must not do
    # quietly — see ``routes/suppression.py``. Opting out is possible and loud.
    suppress: bool = True


class BulkDeleteResult(BaseModel):
    businesses_deleted: int
    contacts_deleted: int
    suppressions_added: int
    numbers_suppressed: list[str] = Field(default_factory=list)
    domains_suppressed: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PreferenceUpdate(BaseModel):
    number_preference: NumberPreference

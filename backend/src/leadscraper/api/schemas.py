"""Request and response shapes for the §13 screens.

Kept separate from the ORM on purpose: §12.1's column set is the contract the
frontend codes against, and a table row is a *projection* (``export/rows.py``)
rather than a serialised ``Business``. Letting the ORM leak into the API would
tie the screen to the schema and make §11 changes breaking changes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from leadscraper.enums import Category, NumberPreference


class SourceFlags(BaseModel):
    """§13 Screen 1's source toggles. Core sources default on, §6 defaults off."""

    google_maps: bool = True
    business_website: bool = True
    directories: bool = True
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

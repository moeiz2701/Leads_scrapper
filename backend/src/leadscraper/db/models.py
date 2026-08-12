"""ORM models for the §11 schema.

Two deliberate departures from the SQL printed in §11, both explained inline:
``businesses.place_id`` is unique per run rather than globally, and ``contacts``
gains a ``belongs_to`` column. Everything else matches column for column.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from leadscraper.db.base import Base, TimestampMixin
from leadscraper.enums import RunStatus


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Run(Base, TimestampMixin):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    mode: Mapped[str] = mapped_column(Text, nullable=False, default="discovery")
    city: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    subcategories: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    number_pref: Mapped[str] = mapped_column(Text, nullable=False)
    sources_enabled: Mapped[dict] = mapped_column(JSONB, nullable=False)
    target_leads: Mapped[int | None] = mapped_column(Integer)
    max_runtime_minutes: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=RunStatus.QUEUED)
    stats: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()

    businesses: Mapped[list[Business]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class Business(Base, TimestampMixin):
    __tablename__ = "businesses"
    __table_args__ = (
        # §11 declares `place_id TEXT UNIQUE`, which is global. That would make a
        # business scrapeable exactly once, ever — the second run of
        # Lahore × salon could not insert any business the first run already saw,
        # and re-running is a normal operation (§16 asks you to validate by
        # re-running). Scoping uniqueness to the run keeps the §10.1 "strong
        # match on place_id" dedupe intact while letting runs repeat.
        UniqueConstraint("run_id", "place_id", name="uq_businesses_run_id_place_id"),
        Index("ix_businesses_run_id_lead_score", "run_id", "lead_score"),
        Index("ix_businesses_place_id", "place_id"),
        Index("ix_businesses_name_norm", "name_norm"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    subcategory: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    area: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    lng: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    place_id: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    facebook_url: Mapped[str | None] = mapped_column(Text)
    instagram_url: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(2, 1))
    review_count: Mapped[int | None] = mapped_column(Integer)
    lead_score: Mapped[int | None] = mapped_column(Integer)
    # §10.1 merge bookkeeping — which business rows folded into this one.
    merged_from: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    run: Mapped[Run] = relationship(back_populates="businesses")
    contacts: Mapped[list[Contact]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        Index("ix_contacts_business_id_rank", "business_id", "rank"),
        Index("ix_contacts_value_e164", "value_e164"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    value_raw: Mapped[str] = mapped_column(Text, nullable=False)
    value_e164: Mapped[str | None] = mapped_column(Text)
    line_type: Mapped[str | None] = mapped_column(Text)
    operator: Mapped[str | None] = mapped_column(Text)
    wa_evidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    wa_label: Mapped[str | None] = mapped_column(Text)
    # WhatsApp evidence routinely comes from a *different* page than the number
    # did: §5.2's whole point is that a business's own site confirms a number
    # Maps published. Without this column that upgrade would silently discard
    # its provenance — `source_url` still points at the Maps listing, and §1
    # requires every record to say which URL its evidence came from.
    wa_evidence_url: Mapped[str | None] = mapped_column(Text)
    person_name: Mapped[str | None] = mapped_column(Text)
    person_role: Mapped[str | None] = mapped_column(Text)
    attribution: Mapped[str | None] = mapped_column(Text)
    # §12.1 column 14 (`phone_N_belongs_to`) has no home in the §11 SQL. Without
    # it the exporter would have to re-derive owner/business/agent from
    # person_role at write time, which loses the distinction between "no name
    # attached" and "known to be the main business line".
    belongs_to: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    scraped_at: Mapped[datetime | None] = mapped_column()

    business: Mapped[Business] = relationship(back_populates="contacts")


class Extraction(Base, TimestampMixin):
    """The extraction ledger — which businesses have already been pulled for outreach.

    Not in §11, and not a compliance record: ``do_not_contact`` is that, and it is
    a different statement. This one answers "have I already handed these numbers
    to whoever does the messaging?", so that Extract → *top 30* twice in a row
    gives 60 distinct businesses rather than the same 30 again.

    ``numbers`` is the batch as it was actually copied, not a live view of the
    business's contacts. The two diverge the moment a later run adds evidence,
    and the operator needs the historical answer — "what did I send out?" — not
    the current one.
    """

    __tablename__ = "extractions"
    __table_args__ = (
        Index("ix_extractions_run_id", "run_id"),
        Index("ix_extractions_batch_id", "batch_id"),
        Index("ix_extractions_batch", "batch"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Unique: a business is extracted or it is not. Re-extracting the same row
    # is the thing this table exists to prevent, so the database says so rather
    # than the service remembering to check.
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # Denormalised from the business so the list can be scoped to a run without
    # a join, and so a per-run "clear all" is one statement.
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    # One Extract click. Lets the list group a pull the operator remembers making.
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The requested size, or NULL for an "extract all" pull — "top 100" and
    # "everything that was left in this view" are different facts about how the
    # row was chosen.
    batch_size: Mapped[int | None] = mapped_column(Integer)
    # _BATCH_SPEC's outreach batch as it stood at pull time — which message this
    # business got. Recorded rather than derived on read for the same reason
    # ``numbers`` is: a business that gains a website moves batch, and the
    # history of what was already sent must not move with it. NULL on rows
    # written before this column existed, and left NULL — see
    # ``services/extraction.list_extractions``.
    batch: Mapped[str | None] = mapped_column(Text)
    numbers: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    extracted_at: Mapped[datetime | None] = mapped_column()

    business: Mapped[Business] = relationship()
    run: Mapped[Run] = relationship()


class RawFetch(Base):
    """§7 raw archive index. Bodies live on disk; this table is the manifest."""

    __tablename__ = "raw_fetches"

    url_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(Text)
    body_path: Mapped[str | None] = mapped_column(Text)
    body_bytes: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(Text)  # listing | detail
    fetched_at: Mapped[datetime | None] = mapped_column()
    expires_at: Mapped[datetime | None] = mapped_column()


class DoNotContact(Base, TimestampMixin):
    """§15 suppression list.

    §15 requires this in v1 and warns that retrofitting it is painful, but the
    §11 SQL omits it — so it is added here. Checked at export time and it
    survives re-runs, which is the whole point: a removal request must not be
    undone by the next scrape.
    """

    __tablename__ = "do_not_contact"

    id: Mapped[uuid.UUID] = _uuid_pk()
    value_e164: Mapped[str | None] = mapped_column(Text, index=True)
    email: Mapped[str | None] = mapped_column(Text, index=True)
    domain: Mapped[str | None] = mapped_column(Text, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    added_by: Mapped[str | None] = mapped_column(Text)


class SourceState(Base):
    """§7 circuit breaker state, surfaced as the §13 Screen 2 status pills."""

    __tablename__ = "source_state"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_empty_reveals: Mapped[int] = mapped_column(Integer, default=0)
    requests_today: Mapped[int] = mapped_column(Integer, default=0)
    paused_until: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column()

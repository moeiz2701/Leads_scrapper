from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming convention so Alembic autogenerate produces stable, diffable
# constraint names instead of Postgres' defaults.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # §11 specifies TIMESTAMPTZ throughout. A bare `Mapped[datetime]` maps to
    # TIMESTAMP WITHOUT TIME ZONE, which reads back naive and then explodes the
    # moment it is compared against an aware `datetime.now(UTC)` — as the cache
    # TTL check does on every single request. Setting it here rather than per
    # column means a future model cannot reintroduce the bug by omission.
    type_annotation_map = {datetime: DateTime(timezone=True)}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

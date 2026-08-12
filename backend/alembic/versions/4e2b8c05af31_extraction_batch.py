"""record the outreach batch on each extraction

Nullable, and deliberately not backfilled. Rows written before this column
existed were pulled before the cascade existed; computing a batch for them now
would assert a fact about a past pull from present data. ``list_extractions``
falls back to the *current* batch for those rows and says which it is showing.

Revision ID: 4e2b8c05af31
Revises: c7f1a4be2d19
Create Date: 2026-08-13 09:12:44.108117
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '4e2b8c05af31'
down_revision: str | None = 'c7f1a4be2d19'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('extractions', sa.Column('batch', sa.Text(), nullable=True))
    op.create_index('ix_extractions_batch', 'extractions', ['batch'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_extractions_batch', table_name='extractions')
    op.drop_column('extractions', 'batch')

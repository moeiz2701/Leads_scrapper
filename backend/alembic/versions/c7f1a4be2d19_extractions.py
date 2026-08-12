"""extractions ledger

Revision ID: c7f1a4be2d19
Revises: 9aacb52b7301
Create Date: 2026-08-11 10:04:12.331920
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c7f1a4be2d19'
down_revision: str | None = '9aacb52b7301'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'extractions',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('batch_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('batch_size', sa.Integer(), nullable=True),
        sa.Column('numbers', postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column('extracted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['business_id'],
            ['businesses.id'],
            name=op.f('fk_extractions_business_id_businesses'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['run_id'],
            ['runs.id'],
            name=op.f('fk_extractions_run_id_runs'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_extractions')),
        sa.UniqueConstraint('business_id', name=op.f('uq_extractions_business_id')),
    )
    op.create_index('ix_extractions_batch_id', 'extractions', ['batch_id'], unique=False)
    op.create_index('ix_extractions_run_id', 'extractions', ['run_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_extractions_run_id', table_name='extractions')
    op.drop_index('ix_extractions_batch_id', table_name='extractions')
    op.drop_table('extractions')

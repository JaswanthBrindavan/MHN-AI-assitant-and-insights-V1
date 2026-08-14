"""phase 7 add soft_deleted_at to pedigree_conditions

Revision ID: 94cafa15d3a1
Revises: c36de02a7c37
Create Date: 2026-08-14 14:55:45.126813

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '94cafa15d3a1'
down_revision: str | None = 'c36de02a7c37'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'pedigree_conditions',
        sa.Column('soft_deleted_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('pedigree_conditions', 'soft_deleted_at')

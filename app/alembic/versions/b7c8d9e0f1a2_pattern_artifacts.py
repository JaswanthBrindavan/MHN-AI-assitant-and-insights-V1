"""pattern artifacts — precomputed behaviour patterns

Reads never compute: the nightly sweep writes these and /api/v1/patterns
serves them. See db/flyway/V43__davi_pattern_artifacts.sql for the production
DDL this mirrors.

Revision ID: b7c8d9e0f1a2
Revises: a7b8c9d0e1f2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7c8d9e0f1a2"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pattern_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("pattern_key", sa.String(96), nullable=False),
        sa.Column("exposure", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(48), nullable=False),
        sa.Column("lag", sa.String(16), nullable=False),
        sa.Column("enough_data", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("days_with", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("days_without", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("mean_with", sa.Float(), nullable=True),
        sa.Column("mean_without", sa.Float(), nullable=True),
        sa.Column("difference", sa.Float(), nullable=True),
        sa.Column("favourable", sa.Boolean(), nullable=True),
        sa.Column("card", sa.JSON(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="active"),
        sa.Column("superseded_by", sa.Uuid(),
                  sa.ForeignKey("pattern_artifacts.id"), nullable=True),
        sa.Column("computed_for", sa.Date(), nullable=False),
        sa.Column("recompute_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pattern_artifacts_user_id", "pattern_artifacts",
                    ["user_id"])
    op.create_index("ix_pattern_artifacts_pattern_key", "pattern_artifacts",
                    ["pattern_key"])
    op.create_index("ix_pattern_artifacts_content_hash", "pattern_artifacts",
                    ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_pattern_artifacts_content_hash",
                  table_name="pattern_artifacts")
    op.drop_index("ix_pattern_artifacts_pattern_key",
                  table_name="pattern_artifacts")
    op.drop_index("ix_pattern_artifacts_user_id", table_name="pattern_artifacts")
    op.drop_table("pattern_artifacts")

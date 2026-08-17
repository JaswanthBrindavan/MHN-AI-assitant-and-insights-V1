"""long-term user memory (user_memories)

Revision ID: a1b2c3d4e5f6
Revises: 64f2074da7f6
Create Date: 2026-08-17

One AI-owned table (uuid user_id, NO FK to "user", default alembic_version),
following the schema-coexistence rules. Stores cross-session discussion topics
and coarse flags per user — no raw message text.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "64f2074da7f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_memories",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("mem_key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=200), nullable=False),
        sa.Column("mention_count", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "kind", "mem_key", name="uq_user_memory"),
    )
    op.create_index(
        op.f("ix_user_memories_user_id"), "user_memories", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_memories_user_id"), table_name="user_memories")
    op.drop_table("user_memories")

"""turn_feedback — reader verdicts on assistant turns

Closes drawbacks.md 8.2: nothing captured whether a reply was any good.

Production adoption ships as db/flyway/V8__davi_feedback.sql; this revision
builds local and test databases only (davi_alembic_version).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "turn_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Plain uuid, NO FK to "user" — the coexistence rule for Davi tables.
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # No FK to conversation_messages either: feedback must SURVIVE the
        # deletion of the conversation it judges, or clearing history would
        # erase the evidence behind a regression test.
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("receipt_id", sa.Uuid(), nullable=True),
        sa.Column("rating", sa.String(length=8), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=True),
        sa.Column("comment", sa.String(length=2000), nullable=True),
        sa.Column("triaged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_turn_feedback", "turn_feedback", ["user_id", "message_id"], unique=True
    )
    op.create_index(
        "ix_turn_feedback_user_id", "turn_feedback", ["user_id"], unique=False
    )
    # The review queue reads exactly this. It was in the Flyway file only, so
    # production had an index no local or CI database ever built and the query
    # was planned differently in test than in production.
    op.create_index(
        "ix_turn_feedback_untriaged",
        "turn_feedback",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("rating = 'down' AND triaged_at IS NULL"),
        sqlite_where=sa.text("rating = 'down' AND triaged_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_turn_feedback_untriaged", table_name="turn_feedback")
    op.drop_index("ix_turn_feedback_user_id", table_name="turn_feedback")
    op.drop_index("uq_turn_feedback", table_name="turn_feedback")
    op.drop_table("turn_feedback")

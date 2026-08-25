"""deferred erasure requests + job_runs actor attribution + retention indexes

Production adoption ships as db/flyway/V10__davi_erasure_and_actor.sql; this
revision builds local and test databases only (davi_alembic_version).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "erasure_requests",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Plain uuid, NO FK to "user". This row must outlive the account it
        # describes — it is the record that the erasure happened.
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        # Fixed at request time, never recomputed from config at purge time.
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_counts", sa.JSON(), nullable=True),
        sa.Column(
            "source",
            sa.String(length=16),
            server_default=sa.text("'api'"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_erasure_requests_user_id", "erasure_requests", ["user_id"]
    )
    op.create_index(
        "ix_erasure_requests_scheduled_for", "erasure_requests", ["scheduled_for"]
    )
    # At most one PENDING request per user; completed/cancelled history is
    # unconstrained, so the index is partial.
    op.create_index(
        "uq_erasure_requests_pending",
        "erasure_requests",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )

    # WHO caused a job. Nullable: scheduled work has no actor, and NULL means
    # "the system", never "unknown user".
    op.add_column(
        "job_runs", sa.Column("actor_user_id", sa.Uuid(), nullable=True)
    )
    op.create_index("ix_job_runs_actor_user_id", "job_runs", ["actor_user_id"])

    # The retention purge selects oldest-first by created_at on the two largest
    # tables in the schema. Without these it is a sequential scan every night.
    op.create_index(
        "ix_conversation_messages_created_at",
        "conversation_messages",
        ["created_at"],
    )
    op.create_index(
        "ix_rag_turn_receipts_created_at", "rag_turn_receipts", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rag_turn_receipts_created_at", table_name="rag_turn_receipts"
    )
    op.drop_index(
        "ix_conversation_messages_created_at", table_name="conversation_messages"
    )
    op.drop_index("ix_job_runs_actor_user_id", table_name="job_runs")
    op.drop_column("job_runs", "actor_user_id")
    op.drop_index("uq_erasure_requests_pending", table_name="erasure_requests")
    op.drop_index(
        "ix_erasure_requests_scheduled_for", table_name="erasure_requests"
    )
    op.drop_index("ix_erasure_requests_user_id", table_name="erasure_requests")
    op.drop_table("erasure_requests")

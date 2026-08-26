"""clinician review queue for held insights

Closes drawbacks.md 8.7: held_for_review artifacts were seen by nobody, ever.

Production adoption ships as db/flyway/V9__davi_clinician_review.sql; this
revision builds local and test databases only (davi_alembic_version).

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clinician_reviewers",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Plain uuid, NO FK to "user" — the coexistence rule for Davi tables.
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("granted_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_clinician_reviewer_user",
        "clinician_reviewers",
        ["user_id"],
        unique=True,
    )
    op.create_index(
        "ix_clinician_reviewers_user_id",
        "clinician_reviewers",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "insight_review_audit",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reviewer_user_id", sa.Uuid(), nullable=False),
        sa.Column("subject_user_id", sa.Uuid(), nullable=False),
        # No FK to insight_artifacts: the audit must SURVIVE the artifact it
        # describes. A retracted insight is exactly the case where the record
        # of who saw it matters most.
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_insight_review_audit_reviewer",
        "insight_review_audit",
        ["reviewer_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_insight_review_audit_subject",
        "insight_review_audit",
        ["subject_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_insight_review_audit_subject", table_name="insight_review_audit"
    )
    op.drop_index(
        "ix_insight_review_audit_reviewer", table_name="insight_review_audit"
    )
    op.drop_table("insight_review_audit")
    op.drop_index("ix_clinician_reviewers_user_id", table_name="clinician_reviewers")
    op.drop_index("uq_clinician_reviewer_user", table_name="clinician_reviewers")
    op.drop_table("clinician_reviewers")

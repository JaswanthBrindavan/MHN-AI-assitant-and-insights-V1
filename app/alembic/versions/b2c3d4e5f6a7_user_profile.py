"""user profile (consent-gated personalization)

Local/test databases only. On the shared production database Flyway owns ALL
schema — the production DDL for this table ships as
``V21__davi_chat_platform.sql``, adopted into mhn-spring's chain (``db/`` is
gitignored here — mhn-spring owns the file).

Follows the house convention: ``user_id`` is a plain uuid with NO foreign key
to ``"user"``; the only FK is to our own ``consent_ledger``.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("age_band", sa.String(length=16), nullable=True),
        sa.Column("sex", sa.String(length=16), nullable=True),
        sa.Column("communication_style", sa.String(length=16), nullable=True),
        sa.Column("preferred_language", sa.String(length=16), nullable=True),
        sa.Column("chronic_conditions", sa.JSON(), nullable=True),
        sa.Column("current_medications", sa.JSON(), nullable=True),
        sa.Column("allergies", sa.JSON(), nullable=True),
        sa.Column("goals", sa.JSON(), nullable=True),
        sa.Column("is_pregnant", sa.Boolean(), nullable=True),
        sa.Column("consent_grant_id", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["consent_grant_id"], ["consent_ledger.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_user_profile_user"),
    )
    op.create_index(
        op.f("ix_user_profiles_user_id"), "user_profiles", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_profiles_user_id"), table_name="user_profiles")
    op.drop_table("user_profiles")

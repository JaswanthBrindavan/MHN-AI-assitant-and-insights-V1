"""per-user memory document

The assembled memory the assistant carries: one row per user, rebuilt on write,
read with a single primary-key lookup instead of the twenty-odd queries that
otherwise run on every turn.

Derived and rebuildable — losing this table costs a rebuild, never data.

Production adoption is in mhn-spring's V21__davi_chat_platform.sql alongside the
rest of Davi's pending schema. This revision builds local and test databases
only (davi_alembic_version).

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_memory_document",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        # Plain uuid, NO FK to "user" — the coexistence rule for Davi tables.
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        # Rendered at write time and BYTE-STABLE between rebuilds, which is
        # what lets it sit behind a prompt-cache breakpoint.
        sa.Column("prompt_block", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "schema_version",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "token_estimate",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_index(
        "uq_user_memory_document", "user_memory_document", ["user_id"], unique=True
    )
    op.create_index(
        "ix_user_memory_document_user_id", "user_memory_document", ["user_id"]
    )
    # The sweep reads oldest-first to find what needs rebuilding.
    op.create_index(
        "ix_user_memory_document_built_at", "user_memory_document", ["built_at"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_memory_document_built_at", table_name="user_memory_document"
    )
    op.drop_index(
        "ix_user_memory_document_user_id", table_name="user_memory_document"
    )
    op.drop_index("uq_user_memory_document", table_name="user_memory_document")
    op.drop_table("user_memory_document")

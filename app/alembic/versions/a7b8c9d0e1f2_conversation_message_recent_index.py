"""composite index for the newest-messages-of-a-session read

Production adoption is db/flyway/V23__davi_conversation_message_index.sql; this
revision builds local and test databases only (davi_alembic_version).

`assemble_context` reads the newest KEEP_VERBATIM messages of a session on every
turn. It used to read the whole session and slice in Python; bounding it to
`ORDER BY created_at DESC, id DESC LIMIT n` is the right query, but on this
schema it can pick a catastrophic plan: with only `session_id` and `created_at`
indexed separately, a LIMIT makes walking the created_at index BACKWARD across
the whole table look cheap. One session is a small slice of a multi-tenant
table, so it is not.

This index makes the intended plan correct rather than merely attractive.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_conversation_messages_session_recent"


def upgrade() -> None:
    # DESC is expressed as raw text: SQLite (which builds the test database)
    # accepts it, and it keeps this identical to the Flyway file.
    op.create_index(
        INDEX_NAME,
        "conversation_messages",
        ["session_id", sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        INDEX_NAME, table_name="conversation_messages", if_exists=True
    )

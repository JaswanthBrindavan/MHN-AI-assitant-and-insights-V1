"""One row per user: the assembled memory the assistant carries.

Derived and rebuildable. Every source of truth stays where it is — losing this
table costs a rebuild, never data.

Two fields carry the design:

* ``prompt_block`` is rendered at WRITE time and stored, not computed on read.
  The read path does no rendering, and — the part that matters — the text is
  byte-stable between rebuilds, which is what lets it sit behind a prompt-cache
  breakpoint. A block that varies per turn caches for nobody.
* ``source_hash`` covers everything the document was built from, so identical
  inputs produce no write. Same idea as ``insight_artifacts.content_hash``.

Only the reader's OWN data. Family data stays a live, gated lookup on every
turn, because the family read permission is checked live and a document that
absorbed a relative's insight would survive the revocation that should have
removed it. See project_docs/per-user-memory.md §5.1.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import JSONColumn, UUIDPrimaryKey

# Bump when the document's shape changes in a way a stale row cannot satisfy.
# A row at an older version is treated as stale and rebuilt rather than read,
# which is cheaper than a migration and safer than trusting the shape.
SCHEMA_VERSION = 1


class UserMemoryDocument(Base, UUIDPrimaryKey):
    """The reader's assembled memory, ready to drop into a prompt."""

    __tablename__ = "user_memory_document"
    __table_args__ = (
        sa.UniqueConstraint("user_id", name="uq_user_memory_document"),
    )

    # Plain uuid, NO FK to "user" — the coexistence rule for Davi tables.
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)

    # The structured snapshot: what was assembled, with provenance.
    document: Mapped[dict] = mapped_column(JSONColumn, nullable=False)

    # The rendered text. Byte-stable between rebuilds — see the module
    # docstring. This is what reaches the model.
    prompt_block: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # What it was built from. Identical inputs => no write.
    source_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    built_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True
    )
    schema_version: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, default=SCHEMA_VERSION
    )
    # Estimated tokens of prompt_block, recorded at build time. Every one of
    # these is charged on every turn, forever — so the number that governs the
    # bill is worth storing rather than recomputing to find out.
    token_estimate: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )

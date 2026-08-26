"""Shared column types and mixins for ORM models."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

_clock_lock = threading.Lock()
_last_now: datetime | None = None


def utcnow() -> datetime:
    """UTC now, STRICTLY INCREASING within this process.

    The system clock is coarser than a burst of inserts — on Windows
    ``datetime.now()`` can return the identical value for a whole batch. Rows
    written in one tick then share ``created_at``, ordering falls back to the
    random uuid4 primary key, and message order becomes nondeterministic.

    That is not cosmetic: ``conversation_messages`` is ordered by
    ``(created_at, id)`` in six places, including ``_ordered_messages``, which
    decides the recent turns the model is shown AND which messages compaction
    folds. A tie there silently reorders the conversation and can point
    ``covers_through_message_id`` at the wrong message.

    Bumping by a microsecond on a tie makes insertion order recoverable from
    the timestamp alone, with no schema change. Drift is bounded by the write
    rate (a microsecond per tied row) and is irrelevant at any real load.
    """
    global _last_now
    with _clock_lock:
        now = datetime.now(UTC)
        if _last_now is not None and now <= _last_now:
            now = _last_now + timedelta(microseconds=1)
        _last_now = now
        return now


# Portable embedding column: pgvector on PostgreSQL, JSON fallback on sqlite
# (tests) where the embedding is always NULL anyway.
EmbeddingType = Vector(1024).with_variant(sa.JSON(), "sqlite")

# Portable JSON column: jsonb on PostgreSQL (matches the existing schema's
# house style), plain JSON on sqlite for unit tests.
JSONColumn = sa.JSON().with_variant(JSONB(), "postgresql")


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4
    )


class CreatedAt:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, nullable=False
    )

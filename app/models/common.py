"""Shared column types and mixins for ORM models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


# Portable embedding column: pgvector on PostgreSQL, JSON fallback on sqlite
# (tests) where the embedding is always NULL anyway.
EmbeddingType = Vector(1024).with_variant(sa.JSON(), "sqlite")


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4
    )


class CreatedAt:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, nullable=False
    )

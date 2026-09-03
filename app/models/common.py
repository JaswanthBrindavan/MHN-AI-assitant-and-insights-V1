"""Shared column types and mixins for ORM models."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import overload

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


@overload
def as_utc(value: datetime) -> datetime: ...
@overload
def as_utc(value: None) -> None: ...
def as_utc(value: datetime | None) -> datetime | None:
    """Normalise a stored timestamp to UTC-aware.

    SQLite (unit tests) hands back NAIVE datetimes even for a
    ``DateTime(timezone=True)`` column, while PostgreSQL returns aware ones.
    Comparing the two raises, so a comparison that works in production blows up
    on the suite -- or, worse, the other way round. Everything this backend
    writes goes through ``utcnow()``, which is UTC, so attaching the zone is
    safe rather than a guess.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


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


# --------------------------------------------------------------------------- #
# Calendar days — the zone mhn-spring resolved them in
# --------------------------------------------------------------------------- #
# `utcnow()` above answers "when". These answer "which DAY", which is a
# different question and has bitten this repo repeatedly.
#
# mhn-spring stamps a calendar day at WRITE time on every rollup
# (`lifestyle_daily_total.bucket_start`, `sahha_daily_total`,
# `lifestyle_limit.effective_from`, `body_measurement_goal.effective_from`)
# using its `app.tracking.zone` property. Reading those with `utcnow().date()`
# asks for the wrong day for every hour the two zones disagree — 5.5 of every
# 24 at +05:30. Measured, not theorised: a symptom ticked today vanished from
# the health summary for that whole span, and a test carrying the same wrong
# clock passed anyway.
#
# The offset is `Settings.tracking_zone_offset_minutes`, not a constant here,
# because assuming a value for another service's property is what caused the
# bug in the first place.
#
# NOT for age from a date of birth. A birthday is a fact about a person, not a
# bucket mhn-spring wrote, and moving it into this zone would add a dependency
# to shift an integer number of years by at most a day.
def tracking_zone() -> timezone:
    """The zone mhn-spring resolves calendar days in."""
    from app.config import get_settings

    return timezone(timedelta(minutes=get_settings().tracking_zone_offset_minutes))


def tracking_today() -> date:
    """Today as the day-bucketed tables reckon it, not as UTC does."""
    return datetime.now(tracking_zone()).date()


def tracking_day_bounds(day: date) -> tuple[datetime, datetime]:
    """The UTC instants a tracking-zone calendar day starts and ends at.

    For the sources that are TIMESTAMPS rather than calendar dates — comparing
    `date(created_at)` in UTC against a tracking-zone day is the same mistake
    pointing the other way. A half-open range converted from the zone is exact.
    """
    start = datetime.combine(day, time.min, tzinfo=tracking_zone())
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)

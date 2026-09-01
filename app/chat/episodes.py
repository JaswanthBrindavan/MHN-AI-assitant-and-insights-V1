"""Symptom episodes — so "still not better" means something.

`ActiveSymptomState` has existed in the models since the beginning and NOTHING
has ever written to it. Without it, a reader reporting a fever on Monday and
saying "still no better" on Thursday is answered as if Thursday were the first
they had mentioned it — which is the difference between a health assistant and
a search box.

An episode is deliberately thin: what, at what severity, first seen, last seen.
No diagnosis, no inference. The clinical judgement stays where it already is —
the triage floor decides severity, and this only remembers what the floor said.

Everything here fails open: episode bookkeeping must never cost a reader an
answer.
"""

from __future__ import annotations

import logging
import re as _re
import uuid
from dataclasses import dataclass
from datetime import UTC, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ActiveSymptomState
from app.models.common import utcnow
from app.triage.red_flags import LEVEL_ORDER, NONE, max_level

logger = logging.getLogger("davi.episodes")

# An episode nobody has mentioned for this long is over. Chosen so a weekly
# check-in still lands inside the same episode, and a new complaint a month
# later is treated as new.
STALE_AFTER = timedelta(days=14)

# How many open episodes to carry into a prompt. More than a handful is noise,
# and the most recent are the ones a follow-up is about.
RECALL_LIMIT = 5


def _aware(value):
    """Normalise a stored timestamp to UTC-aware.

    SQLite (unit tests) hands back NAIVE datetimes even for a
    DateTime(timezone=True) column, while PostgreSQL returns aware ones.
    Comparing the two raises. Everything written here goes through utcnow(),
    which is UTC, so attaching the zone is safe rather than a guess.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Episode:
    symptom: str
    risk_level: str
    first_seen: object
    last_seen: object

    @property
    def days_open(self) -> int:
        try:
            return max(0, (self.last_seen - self.first_seen).days)  # type: ignore[operator]
        except Exception:  # noqa: BLE001
            return 0


async def open_or_touch(
    db: AsyncSession,
    user_id: uuid.UUID,
    symptom: str,
    risk_level: str,
) -> None:
    """Record that a symptom was mentioned. Never raises.

    An existing episode is TOUCHED (last_seen bumped, severity raised if the
    floor says worse). Severity only ever goes up within an episode, mirroring
    the triage floor's own rule: downstream may raise a level, never lower it.
    """
    symptom = (symptom or "").strip().lower()[:128]
    if not symptom:
        return
    try:
        existing = (
            await db.execute(
                select(ActiveSymptomState).where(
                    ActiveSymptomState.user_id == user_id,
                    ActiveSymptomState.symptom == symptom,
                )
            )
        ).scalars().first()

        now = utcnow()
        if existing is not None:
            existing.risk_level = max_level(existing.risk_level, risk_level)
            existing.last_seen_at = now
        else:
            db.add(
                ActiveSymptomState(
                    user_id=user_id,
                    symptom=symptom,
                    risk_level=risk_level,
                    last_seen_at=now,
                )
            )
        await db.flush()
    except Exception:  # noqa: BLE001 — bookkeeping must never cost an answer
        logger.warning("episode open/touch failed", exc_info=True)


# Recovery phrasing (DRAFT) — deterministic, same one-vocabulary spirit as
# triage. English + Hinglish; the i18n tables can extend this later.
_RECOVERY_RE = _re.compile(
    r"\b(?:feeling|feel|much|lot|is|are|it'?s|its)\s+(?:a lot |much )?better"
    r"(?:\s+now)?\b"
    r"|\bbetter now\b|\ball better\b|\b(?:is|it'?s|its) gone\b"
    r"|\bresolved\b|\bsubsided\b|\bcleared up\b|\brecovered\b"
    r"|\bno (?:longer|more) (?:hurts?|paining|there)\b"
    r"|\btheek ho gay[ai]\b|\bthik ho gay[ai]\b|\baram (?:hai|aa gaya)\b",
    _re.IGNORECASE,
)


def is_recovery_message(message: str) -> bool:
    """The reader is saying a symptom improved — a close-out signal, not a
    fresh complaint."""
    return bool(_RECOVERY_RE.search(message or ""))


async def resolve(db: AsyncSession, user_id: uuid.UUID, symptom: str) -> bool:
    """Close an episode because the reader says it is better. Never raises."""
    symptom = (symptom or "").strip().lower()[:128]
    if not symptom:
        return False
    try:
        result = await db.execute(
            delete(ActiveSymptomState).where(
                ActiveSymptomState.user_id == user_id,
                ActiveSymptomState.symptom == symptom,
            )
        )
        await db.flush()
        return bool(getattr(result, "rowcount", 0))
    except Exception:  # noqa: BLE001
        logger.warning("episode resolve failed", exc_info=True)
        return False


async def open_episodes(
    db: AsyncSession, user_id: uuid.UUID, limit: int = RECALL_LIMIT
) -> list[Episode]:
    """Episodes still considered open, most recently mentioned first.

    Stale ones are filtered on read rather than deleted on a timer: a chat turn
    is the wrong place to run a cleanup, and the nightly sweep is the right one.
    """
    try:
        rows = (
            await db.execute(
                select(ActiveSymptomState)
                .where(ActiveSymptomState.user_id == user_id)
                .order_by(ActiveSymptomState.last_seen_at.desc())
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001
        logger.warning("episode read failed", exc_info=True)
        return []

    cutoff = utcnow() - STALE_AFTER
    fresh: list[Episode] = []
    for row in rows:
        last_seen = _aware(row.last_seen_at)
        if last_seen is None or last_seen < cutoff:
            continue
        fresh.append(
            Episode(
                symptom=row.symptom,
                risk_level=row.risk_level,
                first_seen=_aware(row.created_at) or last_seen,
                last_seen=last_seen,
            )
        )
        if len(fresh) >= limit:
            break
    return fresh


async def purge_stale(db: AsyncSession) -> int:
    """Delete episodes past STALE_AFTER. For the nightly sweep."""
    cutoff = utcnow() - STALE_AFTER
    result = await db.execute(
        delete(ActiveSymptomState).where(
            ActiveSymptomState.last_seen_at < cutoff
        )
    )
    await db.flush()
    return getattr(result, "rowcount", 0) or 0


def render_for_prompt(episodes: list[Episode]) -> str:
    """Render open episodes for the [P] block, or "" when there are none.

    Framed as "mentioned before", not as a diagnosis or an active condition —
    the reader said it, that is all this records.
    """
    if not episodes:
        return ""
    parts = []
    for ep in episodes:
        age = ep.days_open
        when = "first mentioned today" if age == 0 else f"first mentioned {age} day(s) ago"
        severity = "" if ep.risk_level == NONE else f", assessed {ep.risk_level}"
        parts.append(f"{ep.symptom} ({when}{severity})")
    return (
        "Symptoms the reader has raised in earlier turns and not said are "
        "better yet: " + "; ".join(parts) + ". If this message looks like a "
        "follow-up about one of them, treat it as the SAME ongoing episode "
        "rather than a new complaint — and if something has lasted longer or "
        "worsened, say so plainly and suggest they get it looked at. "
        "Otherwise do NOT bring these up: answer only what was asked. Raise "
        "one unprompted only if it is genuinely urgent and the recent turns "
        "show you have not already raised it — a check-in repeated every turn "
        "stops being heard, and it derails a reader who asked something else."
    )


def worst_level(episodes: list[Episode]) -> str:
    """The highest severity among open episodes."""
    level = NONE
    for ep in episodes:
        if ep.risk_level in LEVEL_ORDER:
            level = max_level(level, ep.risk_level)
    return level

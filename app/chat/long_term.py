"""Long-term, cross-session user memory (deterministic, no LLM).

Records the TOPICS a user discusses (condition codes + display names) and
coarse red-flag terms, deduplicated per user with recency/frequency counters,
and recalls them as a short context line for future sessions. Stores no raw
message text — only topics and flags — so no PHI is persisted here.

Fail-open: recording never raises to the caller; recall returns "" on error.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import UserMemory
from app.models.common import utcnow

logger = logging.getLogger("davi.memory")

# Cap how many long-term items we recall into context.
RECALL_LIMIT = 8


async def record_topics(
    db: AsyncSession,
    user_id: uuid.UUID,
    topics: dict[str, str],
    flags: list[str] | None = None,
) -> None:
    """Upsert discussed topics ({key: display}) and flags for a user.

    Bumps mention_count + last_seen_at on repeat mentions. Never raises.
    """
    try:
        items: list[tuple[str, str, str]] = [
            ("condition_topic", k, v) for k, v in topics.items()
        ]
        items += [("flag", f, f) for f in (flags or [])]
        if not items:
            return
        now = utcnow()
        for kind, key, value in items:
            key = key[:64]
            existing = (
                await db.execute(
                    select(UserMemory).where(
                        UserMemory.user_id == user_id,
                        UserMemory.kind == kind,
                        UserMemory.mem_key == key,
                    )
                )
            ).scalars().first()
            if existing is not None:
                existing.mention_count += 1
                existing.last_seen_at = now
            else:
                db.add(UserMemory(
                    user_id=user_id, kind=kind, mem_key=key,
                    value=value[:200], mention_count=1, last_seen_at=now,
                ))
        await db.flush()
    except Exception:  # noqa: BLE001 — long-term memory must never break a reply
        logger.warning("long-term memory record failed", exc_info=True)


async def recall(db: AsyncSession, user_id: uuid.UUID) -> str:
    """A short [P]-ready line of what the reader has discussed before.

    Empty string for a first-time user. Ordered by recency then frequency.
    """
    try:
        rows = (
            await db.execute(
                select(UserMemory)
                .where(
                    UserMemory.user_id == user_id,
                    UserMemory.kind == "condition_topic",
                )
                .order_by(
                    UserMemory.last_seen_at.desc(),
                    UserMemory.mention_count.desc(),
                )
                .limit(RECALL_LIMIT)
            )
        ).scalars().all()
        if not rows:
            return ""
        topics = ", ".join(r.value for r in rows)
        return (
            "From past conversations, the reader has previously asked about: "
            f"{topics}. (Use only as background; do not assume they have any of "
            "these conditions.)"
        )
    except Exception:  # noqa: BLE001
        logger.warning("long-term memory recall failed", exc_info=True)
        return ""

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

from sqlalchemy import select, update
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
        # One (kind, key) per call: a term matched twice in one message is one
        # mention, and adding the same row twice would trip the unique key.
        items: dict[tuple[str, str], str] = {
            ("condition_topic", k[:64]): v for k, v in topics.items()
        }
        for f in (flags or []):
            items.setdefault(("flag", f[:64]), f)
        if not items:
            return
        now = utcnow()
        # ONE read for the whole turn. This used to SELECT once per topic and
        # once per flag — three conditions and two flags was five sequential
        # round trips, on every turn, on both engines. mem_key is not unique
        # across kinds, so the (kind, key) match is finished in Python.
        rows = (
            await db.execute(
                select(UserMemory).where(
                    UserMemory.user_id == user_id,
                    UserMemory.mem_key.in_({key for _, key in items}),
                )
            )
        ).scalars().all()
        existing = {(r.kind, r.mem_key): r for r in rows}
        bump = [r.id for (kind, key), r in existing.items() if (kind, key) in items]
        if bump:
            # And ONE write for the repeat mentions, instead of an UPDATE per
            # row at flush. The session's copies are synchronised in Python.
            await db.execute(
                update(UserMemory)
                .where(UserMemory.id.in_(bump))
                .values(mention_count=UserMemory.mention_count + 1, last_seen_at=now)
            )
        for (kind, key), value in items.items():
            if (kind, key) not in existing:
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

"""Conversation persistence, deterministic compaction, and context assembly."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.memory import compact_messages, empty_summary, merge_summaries
from app.models.chat import (
    ConversationMessage,
    ConversationSession,
    ConversationSummary,
)

logger = logging.getLogger("davi.memory")

# Keep the most recent messages verbatim; compact when uncompacted exceeds
# the threshold.
KEEP_VERBATIM = 8
COMPACT_THRESHOLD = 20


async def ensure_session(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID | None
) -> uuid.UUID:
    if session_id is not None:
        existing = (
            await db.execute(
                select(ConversationSession).where(
                    ConversationSession.id == session_id
                )
            )
        ).scalars().first()
        if existing is not None:
            return existing.id
        session = ConversationSession(id=session_id, user_id=user_id)
    else:
        session = ConversationSession(user_id=user_id)
    db.add(session)
    await db.flush()
    return session.id


async def add_message(
    db: AsyncSession,
    session_id: uuid.UUID,
    role: str,
    message: str,
    extracted_intent: dict | None = None,
) -> ConversationMessage:
    msg = ConversationMessage(
        session_id=session_id,
        role=role,
        message=message,
        extracted_intent=extracted_intent,
    )
    db.add(msg)
    await db.flush()
    return msg


async def _ordered_messages(
    db: AsyncSession, session_id: uuid.UUID
) -> list[ConversationMessage]:
    return list(
        (
            await db.execute(
                select(ConversationMessage)
                .where(ConversationMessage.session_id == session_id)
                .order_by(
                    ConversationMessage.created_at, ConversationMessage.id
                )
            )
        ).scalars().all()
    )


async def latest_summary(
    db: AsyncSession, session_id: uuid.UUID
) -> ConversationSummary | None:
    return (
        await db.execute(
            select(ConversationSummary)
            .where(ConversationSummary.session_id == session_id)
            .order_by(ConversationSummary.version.desc())
        )
    ).scalars().first()


async def maybe_compact(db: AsyncSession, session_id: uuid.UUID) -> dict | None:
    """Fold messages older than the last KEEP_VERBATIM into a versioned summary.

    Deterministic and never raises (caller relies on fail-open behaviour).
    """
    try:
        summary_row = await latest_summary(db, session_id)
        messages = await _ordered_messages(db, session_id)

        covered_id = (
            summary_row.covers_through_message_id if summary_row else None
        )
        if covered_id is not None:
            ids = [m.id for m in messages]
            start = ids.index(covered_id) + 1 if covered_id in ids else 0
            uncompacted = messages[start:]
        else:
            uncompacted = messages

        if len(uncompacted) <= COMPACT_THRESHOLD:
            return None

        to_fold = uncompacted[:-KEEP_VERBATIM]
        if not to_fold:
            return None

        new_part = compact_messages(
            [{"role": m.role, "message": m.message} for m in to_fold]
        )
        old = summary_row.summary if summary_row else empty_summary()
        merged = merge_summaries(old, new_part)
        version = summary_row.version + 1 if summary_row else 1
        token_estimate = sum(len(m.message) for m in to_fold) // 4

        db.add(
            ConversationSummary(
                session_id=session_id,
                version=version,
                summary=merged,
                covers_through_message_id=to_fold[-1].id,
                token_estimate=token_estimate,
            )
        )
        await db.flush()
        return merged
    except Exception:  # noqa: BLE001 — compaction must never raise
        logger.warning("compaction failed", exc_info=True)
        return None


async def assemble_context(
    db: AsyncSession, session_id: uuid.UUID
) -> tuple[dict | None, list[dict]]:
    """Return (compacted summary or None, last KEEP_VERBATIM messages verbatim)."""
    summary_row = await latest_summary(db, session_id)
    messages = await _ordered_messages(db, session_id)
    last = [
        {"role": m.role, "message": m.message} for m in messages[-KEEP_VERBATIM:]
    ]
    return (summary_row.summary if summary_row else None), last


async def questions_asked(db: AsyncSession, session_id: uuid.UUID) -> int:
    """How many clarifying questions the assistant has already asked.

    A clarifying question is just a reply that ends in a question mark — no
    state machine, no slot filling. The only thing that needs machinery is
    stopping a loop, and that is one COUNT.

    Never raises: a counting failure must not cost the reader an answer, so it
    degrades to "already asked plenty", which suppresses further questions.
    """
    try:
        rows = (
            await db.execute(
                select(ConversationMessage.message).where(
                    ConversationMessage.session_id == session_id,
                    ConversationMessage.role == "assistant",
                )
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001 — never break a reply over a counter
        logger.warning("clarifying-question count failed", exc_info=True)
        return 999
    return sum(1 for m in rows if (m or "").rstrip().endswith("?"))

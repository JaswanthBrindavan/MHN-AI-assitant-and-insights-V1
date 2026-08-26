"""Conversation persistence, deterministic compaction, and context assembly."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.memory import compact_messages, empty_summary, merge_summaries
from app.chat.summarize import merge_prose, summarize_prose
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
            if existing.user_id != user_id:
                # Not this user's session. Every other read path authorizes
                # (GET /chat/sessions/{id}/messages calls authorize_user);
                # this one did not, so passing someone else's session_id
                # loaded THEIR history into your prompt and appended your turn
                # to it. Mint a fresh session rather than leak one — a 403
                # here would break existing clients for a case that should
                # simply never have worked.
                logger.warning("session_id did not belong to the caller")
                return await ensure_session(db, user_id, None)
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


async def _recent_messages(
    db: AsyncSession, session_id: uuid.UUID, limit: int
) -> list[ConversationMessage]:
    """The last `limit` messages, oldest-first.

    `assemble_context` wants the newest few and runs on EVERY turn. Reading the
    whole session to slice the tail in Python costs one row — and one full
    message body — per turn the reader has ever taken, so the cheapest turn in
    a long conversation is still paying for the longest one.

    DESC + LIMIT in SQL, then reversed, keeps the (created_at, id) ordering
    contract that compaction's `covers_through_message_id` depends on: the same
    tuple orders both directions, so the tail selected here is exactly the tail
    `_ordered_messages` would have sliced.
    """
    rows = (
        await db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.session_id == session_id)
            .order_by(
                ConversationMessage.created_at.desc(),
                ConversationMessage.id.desc(),
            )
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(list(rows)))


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


async def maybe_compact(
    db: AsyncSession, session_id: uuid.UUID, provider=None
) -> dict | None:
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

        # Prose alongside the structure, never instead of it. The structured
        # keys are the source of truth for every safety-relevant field; this
        # only adds the nuance the regex extractors cannot see. A summarizer
        # failure loses the prose and keeps the structure — never the reverse.
        if provider is not None:
            prose = await summarize_prose(
                provider, [{"role": m.role, "message": m.message} for m in to_fold]
            )
            merged = merge_prose(merged, prose)
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
    messages = await _recent_messages(db, session_id, KEEP_VERBATIM)
    last = [{"role": m.role, "message": m.message} for m in messages]
    return (summary_row.summary if summary_row else None), last


async def questions_asked(db: AsyncSession, session_id: uuid.UUID) -> int:
    """How many clarifying questions the assistant has already asked.

    A clarifying question is just a reply that ends in a question mark — no
    state machine, no slot filling. The only thing that needs machinery is
    stopping a loop, and that is one COUNT.

    Never raises: a counting failure must not cost the reader an answer, so it
    degrades to "already asked plenty", which suppresses further questions.

    Counted in SQL. It used to pull every assistant message's full TEXT across
    the whole session and count in Python — transferring the entire transcript
    once per turn to learn a single integer.
    """
    try:
        # rtrim(col, chars) mirrors Python's str.rstrip() closely enough for a
        # loop-stopping counter, and the two-argument form exists on both
        # PostgreSQL and SQLite.
        return (
            await db.execute(
                select(func.count())
                .select_from(ConversationMessage)
                .where(
                    ConversationMessage.session_id == session_id,
                    ConversationMessage.role == "assistant",
                    func.rtrim(ConversationMessage.message, " \t\n\r").like("%?"),
                )
            )
        ).scalar() or 0
    except Exception:  # noqa: BLE001 — never break a reply over a counter
        logger.warning("clarifying-question count failed", exc_info=True)
        return 999

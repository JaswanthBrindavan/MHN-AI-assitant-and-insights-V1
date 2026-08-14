"""Phase 6 — compaction integration (persistence, folding, cascade)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.chat.conversation import (
    add_message,
    ensure_session,
    maybe_compact,
)
from app.models.chat import ConversationSession, ConversationSummary

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.mark.asyncio
async def test_25_message_session_produces_summary(db_session):
    session_id = await ensure_session(db_session, USER, None)

    # Early red flag and a medication mention land in the folded region
    # (first 17 of 25 messages are compacted; last 8 stay verbatim).
    await add_message(db_session, session_id, "user", "suddenly I can't breathe")
    await add_message(
        db_session, session_id, "assistant",
        "Please call your local emergency number.",
    )
    await add_message(
        db_session, session_id, "user", "I also take metformin 500 mg daily"
    )
    await add_message(
        db_session, session_id, "assistant",
        "Noted. Discuss any changes with your prescriber.",
    )
    for i in range(21):
        role = "user" if i % 2 == 0 else "assistant"
        await add_message(db_session, session_id, role, f"routine message number {i}")

    summary = await maybe_compact(db_session, session_id)
    await db_session.commit()

    assert summary is not None
    # A summary row is persisted.
    rows = (
        await db_session.execute(
            select(ConversationSummary).where(
                ConversationSummary.session_id == session_id
            )
        )
    ).scalars().all()
    assert len(rows) == 1

    # Sticky content survived into the summary.
    assert "can't breathe" in rows[0].summary["flags"]
    assert "metformin 500 mg" in rows[0].summary["medications"]
    assert rows[0].summary["boundaries"]  # the emergency directive is a boundary


@pytest.mark.asyncio
async def test_no_compaction_under_threshold(db_session):
    session_id = await ensure_session(db_session, USER, None)
    for i in range(10):
        await add_message(db_session, session_id, "user", f"msg {i}")
    assert await maybe_compact(db_session, session_id) is None


@pytest.mark.asyncio
async def test_session_delete_cascades_summaries(db_session):
    session_id = await ensure_session(db_session, USER, None)
    for i in range(25):
        role = "user" if i % 2 == 0 else "assistant"
        await add_message(db_session, session_id, role, f"message {i}")
    await maybe_compact(db_session, session_id)
    await db_session.commit()

    summaries = (
        await db_session.execute(
            select(ConversationSummary).where(
                ConversationSummary.session_id == session_id
            )
        )
    ).scalars().all()
    assert len(summaries) == 1

    session = (
        await db_session.execute(
            select(ConversationSession).where(ConversationSession.id == session_id)
        )
    ).scalars().first()
    await db_session.delete(session)
    await db_session.commit()

    remaining = (
        await db_session.execute(
            select(ConversationSummary).where(
                ConversationSummary.session_id == session_id
            )
        )
    ).scalars().all()
    assert remaining == []

"""Long-term, cross-session user memory."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.chat.long_term import recall, record_topics
from app.chat.orchestrator import handle_chat
from app.llm.fake import FakeProvider
from app.llm.tools import join_system
from app.models.chat import McpChunk, UserMemory

USER = uuid.UUID("44444444-4444-4444-4444-444444444444")


@pytest.mark.asyncio
async def test_record_and_recall(db_session):
    await record_topics(db_session, USER, {"T2DM": "Diabetes mellitus"})
    text = await recall(db_session, USER)
    assert "Diabetes mellitus" in text
    assert "previously asked about" in text


@pytest.mark.asyncio
async def test_record_dedupes_and_counts(db_session):
    await record_topics(db_session, USER, {"T2DM": "Diabetes mellitus"})
    await record_topics(db_session, USER, {"T2DM": "Diabetes mellitus"})
    await record_topics(db_session, USER, {"HTN": "Hypertension"})
    rows = (
        await db_session.execute(
            select(UserMemory).where(UserMemory.user_id == USER)
        )
    ).scalars().all()
    by_key = {r.mem_key: r for r in rows}
    assert by_key["T2DM"].mention_count == 2   # deduped, counted
    assert by_key["HTN"].mention_count == 1
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_recall_empty_for_new_user(db_session):
    assert await recall(db_session, uuid.uuid4()) == ""


@pytest.mark.asyncio
async def test_cross_session_recall_through_orchestrator(db_session):
    db_session.add(McpChunk(
        condition_code="T2DM", chunk_type="symptoms",
        content="Diabetes is a chronic condition.", embedding=None,
    ))
    await db_session.flush()
    provider = FakeProvider(responses=["Info about diabetes [1]."])

    # Session 1 (no session_id) — discusses diabetes.
    await handle_chat(db_session, USER, "tell me about diabetes", provider)

    # A NEW session (different session_id) recalls the prior topic in [P].
    # Read the prompt from `calls`, which both engines record — a spy on
    # `generate` sees nothing on the agentic engine.
    spy = FakeProvider(responses=["General wellbeing info [GK]."])
    await handle_chat(
        db_session, USER, "how do I stay healthy?", spy,
        uuid.uuid4(),  # fresh session
    )
    system = join_system(spy.calls[0]["system"])
    assert "previously asked about" in system
    # Unit env has no condition_registry, so the topic value is the code T2DM
    # (production resolves it to "Diabetes mellitus").
    assert "t2dm" in system.lower()


def _statements(engine) -> list[str]:
    seen: list[str] = []
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):
        seen.append(statement.split()[0].upper())

    return seen


@pytest.mark.asyncio
async def test_recording_a_turn_is_one_read_however_many_items(db_session, engine):
    """Three conditions and two flags used to be five sequential SELECTs, on
    every turn, on both engines. It is one now, and the repeat mentions are
    one UPDATE rather than one per row."""
    topics = {"MC001": "Diabetes", "MC051": "Hypertension", "MC052": "CAD"}
    flags = ["chest pain", "left arm"]

    seen = _statements(engine)
    await record_topics(db_session, USER, topics, flags=flags)
    assert seen.count("SELECT") == 1, seen

    seen.clear()
    await record_topics(db_session, USER, topics, flags=flags)
    assert seen.count("SELECT") == 1, seen
    assert seen.count("UPDATE") == 1, seen

    rows = (
        await db_session.execute(
            select(UserMemory).where(UserMemory.user_id == USER)
        )
    ).scalars().all()
    assert len(rows) == 5
    assert {r.mention_count for r in rows} == {2}


@pytest.mark.asyncio
async def test_a_flag_and_a_topic_with_the_same_key_stay_separate_rows(db_session):
    """mem_key is unique per (user, kind), not per user — the batched read
    matches on both."""
    await record_topics(db_session, USER, {"x": "X"}, flags=["x"])
    await record_topics(db_session, USER, {"x": "X"}, flags=["x", "x"])
    rows = (
        await db_session.execute(
            select(UserMemory).where(UserMemory.user_id == USER)
        )
    ).scalars().all()
    assert sorted((r.kind, r.mention_count) for r in rows) == [
        ("condition_topic", 2), ("flag", 2),
    ]

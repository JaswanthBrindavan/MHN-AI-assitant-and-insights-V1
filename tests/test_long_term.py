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
    class Spy(FakeProvider):
        def __init__(self):
            super().__init__(responses=["General wellbeing info [GK]."])
            self.system = ""

        async def generate(self, *, system, user: str) -> str:
            self.system = join_system(system)
            return "General wellbeing info [GK]."

    spy = Spy()
    await handle_chat(
        db_session, USER, "how do I stay healthy?", spy,
        uuid.uuid4(),  # fresh session
    )
    assert "previously asked about" in spy.system
    # Unit env has no condition_registry, so the topic value is the code T2DM
    # (production resolves it to "Diabetes mellitus").
    assert "t2dm" in spy.system.lower()

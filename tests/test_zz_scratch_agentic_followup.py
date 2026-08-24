"""SCRATCH — reviewer experiment. Delete after."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.chat.orchestrator import handle_chat
from app.llm.fake import FakeProvider
from app.models.chat import McpChunk, UserMemory

USER = uuid.UUID("55555555-5555-5555-5555-555555555555")


@pytest.fixture(autouse=True)
def _agentic(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("CHAT_ENGINE", "agentic")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed(db):
    db.add(McpChunk(
        condition_code="T2DM", chunk_type="symptoms",
        content="Diabetes can lead to serious complications if unmanaged.",
        embedding=None,
    ))
    await db.flush()


@pytest.mark.asyncio
async def test_scratch_two_turn(db_session):
    await _seed(db_session)
    provider = FakeProvider()
    r1 = await handle_chat(db_session, USER, "tell me about diabetes", provider)
    print("TURN1 conditions:", r1.provenance.get("conditions"))
    sys1 = provider.calls[-1]["system"]
    print("TURN1 has chunk block:", "Retrieved knowledge blocks" in sys1)

    r2 = await handle_chat(db_session, USER, "is it serious?", provider, r1.session_id)
    print("TURN2 conditions:", r2.provenance.get("conditions"))
    sys2 = provider.calls[-1]["system"]
    print("TURN2 has chunk block:", "Retrieved knowledge blocks" in sys2)
    print("TURN2 has recent conv:", "Recent conversation" in sys2)
    print("TURN2 mentions diabetes:", "diabetes" in sys2.lower())
    print("TURN2 tools offered:", provider.calls[-1]["tools"])
    print("TURN2 has recall line:", "previously asked about" in sys2)

    rows = (await db_session.execute(
        select(UserMemory).where(UserMemory.user_id == USER)
    )).scalars().all()
    print("UserMemory rows:", [(r.kind, r.mem_key) for r in rows])

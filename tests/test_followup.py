"""Short-term memory: follow-up questions carry topic scope and see recent turns."""

from __future__ import annotations

import uuid

import pytest

from app.chat.orchestrator import handle_chat
from app.llm.fake import FakeProvider
from app.llm.tools import join_system
from app.models.chat import McpChunk

USER = uuid.UUID("33333333-3333-3333-3333-333333333333")


async def _seed_diabetes_chunk(db):
    # In the unit env no condition_registry is ingested, so the static scope
    # map resolves "diabetes" → the legacy engine code T2DM.
    db.add(McpChunk(
        condition_code="T2DM", chunk_type="symptoms",
        content="Diabetes can lead to serious complications if unmanaged.",
        embedding=None,
    ))
    await db.flush()


class _SpyProvider(FakeProvider):
    def __init__(self):
        super().__init__(responses=["Here is some information [1]."])
        self.systems: list[str] = []

    async def generate(self, *, system, user: str) -> str:
        self.systems.append(join_system(system))
        return "Here is some information [1]."


@pytest.mark.asyncio
async def test_followup_carries_topic_scope(db_session):
    await _seed_diabetes_chunk(db_session)
    provider = _SpyProvider()

    # Turn 1: names diabetes → scopes MC001.
    r1 = await handle_chat(db_session, USER, "tell me about diabetes", provider)
    sid = r1.session_id
    assert "T2DM" in (r1.provenance.get("conditions") or [])

    # Turn 2: a follow-up with NO condition keyword — must inherit MC001.
    r2 = await handle_chat(db_session, USER, "is it serious?", provider, sid)
    assert "T2DM" in (r2.provenance.get("conditions") or []), \
        "follow-up lost the topic scope"


@pytest.mark.asyncio
async def test_followup_recent_turns_reach_prompt(db_session):
    await _seed_diabetes_chunk(db_session)
    provider = _SpyProvider()

    r1 = await handle_chat(db_session, USER, "tell me about diabetes", provider)
    await handle_chat(db_session, USER, "is it serious?", provider, r1.session_id)

    # The second turn's system prompt must include the recent conversation so
    # the model can resolve "it".
    second_system = provider.systems[-1]
    assert "Recent conversation" in second_system
    assert "diabetes" in second_system.lower()


@pytest.mark.asyncio
async def test_first_turn_has_no_recent_block(db_session):
    await _seed_diabetes_chunk(db_session)
    provider = _SpyProvider()
    await handle_chat(db_session, USER, "tell me about diabetes", provider)
    # First message of a session → no prior turns → no recent-conversation block.
    assert "Recent conversation" not in provider.systems[-1]


# --------------------------------------------------------------------------- #
# The SAME guarantee, on the agentic engine — production runs CHAT_ENGINE=
# agentic, and `_SpyProvider` above only overrides `.generate`, the LEGACY
# call. `run_agent` calls `.generate_turn` instead, so the two tests above
# have never actually looked at what the engine that answers real users was
# told — an engine-parity gap this codebase has hit before (see
# test_agentic_orchestrator.py). `FakeProvider.generate_turn` records to
# `.calls` regardless of subclass, so no spy override is needed here.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_followup_recent_turns_reach_the_agentic_prompt(
    db_session, monkeypatch
):
    from app.config import get_settings

    monkeypatch.setenv("CHAT_ENGINE", "agentic")
    get_settings.cache_clear()
    try:
        await _seed_diabetes_chunk(db_session)
        provider = FakeProvider(responses=["Here is some information [1]."] * 2)

        r1 = await handle_chat(db_session, USER, "tell me about diabetes", provider)
        await handle_chat(
            db_session, USER, "is it serious?", provider, r1.session_id
        )

        second_system = join_system(provider.calls[-1]["system"])
        assert "Recent conversation" in second_system
        assert "diabetes" in second_system.lower()
    finally:
        get_settings.cache_clear()

"""Clarifying questions — bounded, and never at the cost of an escalation.

A clarifying question is just a reply that ends in a question mark. The only
thing that needs machinery is stopping a loop, so the whole feature is one
COUNT and one prompt rule. No state machine, no slot filling.

The safety property: a red flag must never be met with a question. When someone
describes chest pain, "how long has this been going on?" is the wrong answer.
"""

from __future__ import annotations

import uuid

import pytest

from app.chat.conversation import add_message, ensure_session, questions_asked
from app.chat.orchestrator import handle_chat
from app.llm.fake import FakeProvider
from app.llm.tools import LLMTurn


@pytest.fixture(autouse=True)
def _agentic(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("CHAT_ENGINE", "agentic")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# The counter
# --------------------------------------------------------------------------- #
async def test_a_fresh_session_has_asked_nothing(db_session):
    sid = await ensure_session(db_session, uuid.uuid4(), None)
    assert await questions_asked(db_session, sid) == 0


async def test_only_assistant_questions_count(db_session):
    sid = await ensure_session(db_session, uuid.uuid4(), None)
    await add_message(db_session, sid, "user", "is this serious?")
    await add_message(db_session, sid, "assistant", "It can be. Here is why.")
    assert await questions_asked(db_session, sid) == 0

    await add_message(db_session, sid, "assistant", "How long has this lasted?")
    assert await questions_asked(db_session, sid) == 1


async def test_trailing_whitespace_does_not_hide_a_question(db_session):
    sid = await ensure_session(db_session, uuid.uuid4(), None)
    await add_message(db_session, sid, "assistant", "How long?   \n")
    assert await questions_asked(db_session, sid) == 1


async def test_a_counting_failure_suppresses_questions_rather_than_erroring(
    db_session, monkeypatch
):
    """Fail CLOSED: if we cannot count, assume we have asked enough. A counter
    error must never cost the reader an answer."""
    import app.chat.conversation as conv

    async def _boom(*_a, **_kw):
        raise RuntimeError("db gone")

    monkeypatch.setattr(conv.AsyncSession, "execute", _boom, raising=False)
    sid = uuid.uuid4()
    assert await questions_asked(db_session, sid) >= 999


# --------------------------------------------------------------------------- #
# The prompt rule
# --------------------------------------------------------------------------- #
async def test_the_model_is_invited_to_ask_when_the_budget_allows(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="How long has that been going on?")])
    await handle_chat(db_session, uuid.uuid4(), "I feel dizzy", provider)
    assert "clarifying question" in provider.calls[0]["system"]


async def test_the_invitation_is_withdrawn_once_the_budget_is_spent(db_session):
    user_id = uuid.uuid4()
    sid = await ensure_session(db_session, user_id, None)
    await add_message(db_session, sid, "assistant", "How long has this lasted?")
    await add_message(db_session, sid, "assistant", "And how severe is it?")

    provider = FakeProvider(turns=[LLMTurn(text="Here is what generally helps.")])
    await handle_chat(db_session, user_id, "I feel dizzy", provider, session_id=sid)
    assert "clarifying question" not in provider.calls[0]["system"]


async def test_the_budget_is_configurable(db_session, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("CHAT_MAX_CLARIFYING_QUESTIONS", "0")
    get_settings.cache_clear()

    provider = FakeProvider(turns=[LLMTurn(text="Here is some guidance.")])
    await handle_chat(db_session, uuid.uuid4(), "I feel dizzy", provider)
    assert "clarifying question" not in provider.calls[0]["system"]


# --------------------------------------------------------------------------- #
# Safety: never delay an escalation to ask a question
# --------------------------------------------------------------------------- #
async def test_an_emergency_is_never_met_with_a_question(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="How long have you felt that?")])
    result = await handle_chat(db_session, uuid.uuid4(), "I can't breathe", provider)
    assert not result.response_message.rstrip().endswith("?")
    assert result.risk_level == "emergency"
    assert provider.calls == []


async def test_a_question_reply_still_passes_the_validator(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="How long has that been going on?")])
    result = await handle_chat(db_session, uuid.uuid4(), "I feel dizzy", provider)
    # It survived the guards rather than being swapped for a safe reply.
    assert result.response_message.rstrip().endswith("?")
    assert "degraded" not in result.provenance


async def test_asking_a_question_increments_the_count_for_the_next_turn(db_session):
    user_id = uuid.uuid4()
    provider = FakeProvider(turns=[LLMTurn(text="How long has that been going on?")])
    result = await handle_chat(db_session, user_id, "I feel dizzy", provider)
    assert result.session_id is not None
    assert await questions_asked(db_session, result.session_id) == 1

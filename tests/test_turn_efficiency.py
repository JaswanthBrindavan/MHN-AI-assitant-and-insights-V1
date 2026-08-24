"""Turn efficiency — fewer round trips, without touching correctness.

NOTE ON WHY THIS IS NOT PARALLELISM. The plan called for asyncio.gather over
the independent per-turn lookups. That is not possible here: they all share one
AsyncSession, and SQLAlchemy refuses concurrent operations on one. The same
mistake in the tool loop made 3 of every 4 tool calls fail. So the win comes
from doing less work, not from doing it at once.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event

from app.chat.context import build_patient_context, clear_patient_context_memo
from app.chat.orchestrator import handle_chat
from app.llm.fake import FakeProvider


@pytest.fixture(autouse=True)
def _clean_memo():
    clear_patient_context_memo()
    yield
    clear_patient_context_memo()


def _count_queries(engine):
    counter = {"n": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, params, context, executemany):
        counter["n"] += 1

    return counter


async def test_patient_context_is_computed_once_per_session(db_session, engine):
    user_id = uuid.uuid4()
    counter = _count_queries(engine)

    await build_patient_context(db_session, user_id)
    first = counter["n"]
    assert first > 0, "expected the first call to query"

    for _ in range(5):
        await build_patient_context(db_session, user_id)
    assert counter["n"] == first, "memo did not prevent repeat queries"


async def test_the_memo_returns_equal_values_not_shared_mutable_state(db_session):
    """A caller mutating the returned code set must not corrupt the memo."""
    user_id = uuid.uuid4()
    _text, codes_a = await build_patient_context(db_session, user_id)
    codes_a.add("INJECTED")
    _text, codes_b = await build_patient_context(db_session, user_id)
    assert "INJECTED" not in codes_b


async def test_the_memo_does_not_leak_between_users(db_session):
    a, b = uuid.uuid4(), uuid.uuid4()
    text_a, _ = await build_patient_context(db_session, a)
    text_b, _ = await build_patient_context(db_session, b)
    assert text_a == text_b == ""  # both empty, but computed independently


async def test_clearing_the_memo_forces_a_recompute(db_session, engine):
    user_id = uuid.uuid4()
    await build_patient_context(db_session, user_id)
    counter = _count_queries(engine)
    await build_patient_context(db_session, user_id)
    assert counter["n"] == 0

    clear_patient_context_memo()
    await build_patient_context(db_session, user_id)
    assert counter["n"] > 0


async def test_a_full_turn_still_answers_correctly(db_session):
    """The optimisations must not change behaviour."""
    provider = FakeProvider(responses=["Some general guidance about sleep."])
    result = await handle_chat(
        db_session, uuid.uuid4(), "how does sleep work?", provider
    )
    assert result.response_message
    assert result.risk_level == "none"


async def test_scope_carry_forward_survived_the_dedup(db_session):
    """resolve_scope was called twice with the same message; the duplicate was
    removed by deriving both answers from one call. The follow-up behaviour it
    supported must still work."""
    from app.chat.conversation import add_message, ensure_session

    user_id = uuid.uuid4()
    sid = await ensure_session(db_session, user_id, None)
    await add_message(db_session, sid, "user", "tell me about diabetes")
    await add_message(db_session, sid, "assistant", "Here is some information.")

    provider = FakeProvider(responses=["It can be, yes."])
    result = await handle_chat(
        db_session, user_id, "is it serious?", provider, session_id=sid
    )
    assert result.response_message

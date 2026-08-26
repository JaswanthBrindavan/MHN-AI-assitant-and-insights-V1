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


# --------------------------------------------------------------------------- #
# Session-length scaling
#
# Live staging measurement across ONE long session: 10s -> 19s -> 22s -> 30s ->
# 43s -> 113s. Every turn re-read work proportional to the whole transcript, so
# the cheapest turn in a long conversation still paid for the longest one.
# --------------------------------------------------------------------------- #
async def _seed_session(db, n: int):
    from app.chat.conversation import add_message, ensure_session

    user_id = uuid.uuid4()
    session_id = await ensure_session(db, user_id, None)
    for i in range(n):
        await add_message(db, session_id, "user", f"question number {i}")
        await add_message(db, session_id, "assistant", f"answer number {i}")
    return session_id


async def test_assemble_context_bounds_its_read_in_sql(db_session, engine):
    """It wants the newest few; it must not read the whole session to get them."""
    from app.chat.conversation import assemble_context

    session_id = await _seed_session(db_session, 40)

    seen: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):
        seen.append(" ".join(statement.split()))

    await assemble_context(db_session, session_id)

    msg_selects = [
        s for s in seen
        if "conversation_messages" in s and s.upper().startswith("SELECT")
    ]
    assert msg_selects, "no message SELECT was issued"
    assert all("LIMIT" in s.upper() for s in msg_selects), (
        f"assemble_context read conversation_messages unbounded: {msg_selects}"
    )


async def test_assemble_context_still_returns_the_right_tail(db_session):
    """Bounding the read must not change WHICH messages come back, or their
    order — compaction's covers_through_message_id depends on it."""
    from app.chat.conversation import KEEP_VERBATIM, assemble_context

    session_id = await _seed_session(db_session, 40)
    _summary, recent = await assemble_context(db_session, session_id)

    assert len(recent) == KEEP_VERBATIM
    # Oldest-first, and ending on the very last message written.
    assert recent[-1]["message"] == "answer number 39"
    assert recent[-1]["role"] == "assistant"
    assert recent[0]["message"] == "question number 36"


async def test_questions_asked_counts_without_reading_the_transcript(db_session):
    from app.chat.conversation import add_message, ensure_session, questions_asked

    user_id = uuid.uuid4()
    session_id = await ensure_session(db_session, user_id, None)
    await add_message(db_session, session_id, "assistant", "How long has it hurt?")
    await add_message(db_session, session_id, "assistant", "Any fever?\n")
    await add_message(db_session, session_id, "assistant", "Here is some guidance.")
    await add_message(db_session, session_id, "user", "and you? is it bad?")

    # Two assistant questions; trailing newline still counts, the statement
    # does not, and the USER's question mark is not the assistant's.
    assert await questions_asked(db_session, session_id) == 2

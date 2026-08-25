"""A database connection must not be held idle across an LLM call.

This is the constraint that decides how many concurrent chats the service can
serve, and it is not obvious from reading the code: the endpoint commits AFTER
`handle_chat` returns (`app/api/v1/chat.py`), so a transaction opened by the
first write stays open across every provider call in the turn — several seconds
of network wait with a pooled connection pinned to it.

The arithmetic (derived, see project_docs/per-user-memory.md): at a 3 s turn
and a 4x diurnal peak, ~167 concurrent connections are needed at 1M users and
~1,667 at 10M, against a SQLAlchemy default pool of 5 + 10 per process — on a
database shared with mhn-spring and mhn-ai. PgBouncer cannot help: in
transaction mode it cannot release a transaction that spans the LLM call.

These tests measure `AsyncSession.in_transaction()` at the moment the provider
is called. An open transaction is precisely what pins a pooled connection, and
unlike pool counters it means the same thing on every dialect.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.chat.orchestrator import handle_chat
from app.config import get_settings
from app.llm.fake import FakeProvider
from app.llm.tools import LLMTurn
from app.models.chat import ConversationMessage, ConversationSession

USER = uuid.UUID("00000000-0000-0000-0000-0000000ab1e0")

_REPLY = "Tiredness has many ordinary causes [GK]. Speak to a clinician."


class TransactionWatchingProvider(FakeProvider):
    """Records whether a transaction was open during each provider call.

    A provider call is exactly the moment the connection should be back in the
    pool: the request is waiting on the network, not on the database.
    """

    def __init__(self, db, **kwargs):
        super().__init__(**kwargs)
        self._db = db
        self.in_transaction_during_call: list[bool] = []

    def _sample(self) -> None:
        self.in_transaction_during_call.append(bool(self._db.in_transaction()))

    async def generate(self, *, system, user: str) -> str:
        self._sample()
        return _REPLY

    async def generate_turn(self, *, system, messages, tools=()):
        self._sample()
        return LLMTurn(text=_REPLY)


@pytest.mark.parametrize("engine_name", ["legacy", "agentic"])
async def test_no_transaction_is_open_across_a_provider_call(
    db_session, monkeypatch, engine_name
):
    """The core property. Fails before the commit-placement fix.

    If this regresses, the symptom in production is not a test failure — it is
    connection-pool exhaustion that takes down the two services sharing this
    database.
    """
    monkeypatch.setattr(get_settings(), "chat_engine", engine_name)
    provider = TransactionWatchingProvider(db_session)

    await handle_chat(
        db_session, USER, "why am I so tired?", provider, uuid.uuid4()
    )

    assert provider.in_transaction_during_call, "the provider was never called"
    held = [x for x in provider.in_transaction_during_call if x]
    assert not held, (
        f"{len(held)} of {len(provider.in_transaction_during_call)} provider "
        f"call(s) ran inside an open transaction "
        f"({provider.in_transaction_during_call}). The connection is pinned "
        f"for the whole LLM round-trip."
    )


async def test_the_question_is_already_committed_when_the_provider_fails(
    db_session, monkeypatch
):
    """The durability the early commit buys, stated as the property that holds.

    An earlier version of this test called `db_session.commit()` itself and so
    passed with the fix removed entirely — it proved nothing. What actually
    discriminates is the transaction state AT the moment the provider is
    asked: if no transaction is open, the reader's message is already durable,
    so the failure that follows cannot take it with it.
    """
    monkeypatch.setattr(get_settings(), "chat_engine", "legacy")
    observed: dict = {}

    class BrokenProvider(FakeProvider):
        async def generate(self, *, system, user: str) -> str:
            observed["in_transaction"] = bool(db_session.in_transaction())
            raise RuntimeError("provider is down")

    result = await handle_chat(
        db_session, USER, "why am I so tired?", BrokenProvider(), uuid.uuid4()
    )

    assert observed["in_transaction"] is False, (
        "the question was still uncommitted when the provider failed"
    )
    # Fail-open: the reader still gets an answer.
    assert result.response_message
    assert result.provenance.get("degraded") == "provider_error"


async def test_the_whole_turn_is_still_persisted(db_session, monkeypatch):
    """Releasing the connection must not cost us the transcript.

    Committing at more points means more places to get persistence wrong. Both
    turns must survive, in order.
    """
    monkeypatch.setattr(get_settings(), "chat_engine", "legacy")
    session_id = uuid.uuid4()

    await handle_chat(
        db_session, USER, "why am I so tired?", FakeProvider(), session_id
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.session_id == session_id)
            .order_by(ConversationMessage.created_at)
        )
    ).scalars().all()
    assert [r.role for r in rows] == ["user", "assistant"]
    assert rows[0].message == "why am I so tired?"
    assert rows[1].message


# --------------------------------------------------------------------------- #
# The savepoint boundary
# --------------------------------------------------------------------------- #
async def test_a_release_never_escapes_a_savepoint(db_session):
    """`in_transaction()` is True inside a SAVEPOINT, and `commit()` releases it.

    Four scripts (cost_report, live_analytics, stress_10k, stress_correlation)
    wrap `handle_chat` in `begin_nested()` + `rollback()` so synthetic traffic
    leaves no trace. A release inside that savepoint would make thousands of
    fabricated chats PERMANENT in a database shared with two other services,
    and then raise ResourceClosedError on their rollback.

    Failing safe means keeping the connection -- merely the old behaviour --
    rather than destroying the caller's isolation boundary.
    """
    from app.chat.db_release import ReleasingProvider

    provider = ReleasingProvider(FakeProvider(), db_session)
    savepoint = await db_session.begin_nested()
    # A ConversationSession has no FK, so this needs no parent row.
    db_session.add(ConversationSession(user_id=USER))
    await db_session.flush()
    assert db_session.in_nested_transaction()

    await provider.generate(system="s", user="u")

    assert savepoint.is_active, (
        "the release committed inside a savepoint: the caller's rollback "
        "boundary is gone and their synthetic writes are now permanent"
    )
    await savepoint.rollback()


async def test_the_scripts_isolation_pattern_still_works(db_session):
    """End-to-end shape of the four scripts: savepoint, chat, rollback."""
    from sqlalchemy import func

    savepoint = await db_session.begin_nested()
    await handle_chat(
        db_session, USER, "why am I so tired?", FakeProvider(), uuid.uuid4()
    )
    await savepoint.rollback()

    remaining = (
        await db_session.execute(select(func.count(ConversationMessage.id)))
    ).scalar()
    assert remaining == 0, (
        f"{remaining} row(s) survived the rollback -- synthetic traffic would "
        f"be permanent in the shared production database"
    )

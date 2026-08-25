"""The session list must not aggregate the whole message table.

`GET /chat/sessions` builds a per-session count/last-seen aggregate. The
obvious shape groups `conversation_messages` in full and then outer-joins the
caller's sessions — but the caller's filter sits on the LEFT side of an outer
join, so it cannot be pushed into the subquery. The database aggregates every
message ever sent by anybody before the 50-row limit can apply.

That is a cost that grows with total product traffic rather than with the
caller's own history, on an endpoint the UI hits on every load. Derived: a
timeout around 100K users.

Correctness was never wrong, which is why no existing test caught it. This one
asserts the SHAPE of the query, because the shape is the defect.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event

from app.auth import DEV_USER_ID
from app.models.chat import ConversationMessage, ConversationSession

OTHER = uuid.UUID("00000000-0000-0000-0000-00000000beef")


async def _seed(sessionmaker, user_id: uuid.UUID, n_messages: int = 2):
    async with sessionmaker() as db:
        session = ConversationSession(user_id=user_id)
        db.add(session)
        await db.flush()
        for i in range(n_messages):
            db.add(
                ConversationMessage(
                    session_id=session.id,
                    role="user" if i % 2 == 0 else "assistant",
                    message=f"message {i}",
                )
            )
        await db.commit()
        return session.id


@pytest.fixture
def captured_sql(engine):
    """Every statement the endpoint issues."""
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):
        statements.append(" ".join(statement.split()))

    yield statements
    event.remove(engine.sync_engine, "before_cursor_execute", _capture)


async def test_the_aggregate_is_scoped_before_it_groups(
    client, sessionmaker, captured_sql
):
    """The GROUP BY subquery must carry its own predicate.

    Without one, `count(...) GROUP BY session_id` scans and groups the entire
    table before anything filters it.
    """
    await _seed(sessionmaker, DEV_USER_ID)
    captured_sql.clear()

    response = await client.get("/api/v1/chat/sessions")
    assert response.status_code == 200

    grouped = [s for s in captured_sql if "GROUP BY" in s.upper()]
    assert grouped, "no aggregate was issued — has the endpoint changed?"
    for statement in grouped:
        head = statement.upper().split("GROUP BY")[0]
        assert "WHERE" in head, (
            "the aggregate groups the whole conversation_messages table before "
            "any filter applies:\n" + statement
        )


async def test_another_users_messages_are_not_aggregated(
    client, sessionmaker, captured_sql
):
    """Correctness, which was already right — pinned so the fix cannot regress
    into a version that filters late again."""
    await _seed(sessionmaker, DEV_USER_ID, n_messages=2)
    await _seed(sessionmaker, OTHER, n_messages=40)

    items = (await client.get("/api/v1/chat/sessions")).json()
    assert len(items) == 1
    assert items[0]["message_count"] == 2, (
        f"got {items[0]['message_count']} — another user's messages were counted"
    )


async def test_the_caller_sees_only_their_own_sessions(client, sessionmaker):
    await _seed(sessionmaker, OTHER)
    assert (await client.get("/api/v1/chat/sessions")).json() == []

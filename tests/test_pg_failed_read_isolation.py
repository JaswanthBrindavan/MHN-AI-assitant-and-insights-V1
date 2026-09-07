"""A failed read inside a SAVEPOINT must not poison the rest of the turn.

Marked `pg` because this is a PostgreSQL behaviour: a failed statement aborts
the whole transaction, and every later statement fails with "current
transaction is aborted". SQLite tolerates a failed statement, so the missing
savepoints this guards against were invisible to the ordinary suite.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError

from app.chat.data_handlers import handle_about_me_query
from app.chat.episodes import open_episodes
from app.chat.orchestrator import handle_chat
from app.llm.fake import FakeProvider
from app.models.core import User
from app.triage.red_flags import HIGH
from tests.test_about_me import USER, _seed

pytestmark = pytest.mark.pg


async def test_a_failed_episode_read_does_not_abort_the_transaction(pg_session):
    await pg_session.execute(text("DROP TABLE active_symptom_states"))

    with pytest.raises(ProgrammingError):
        await open_episodes(pg_session, uuid.uuid4())

    # Without the savepoint this raises InFailedSqlTransactionError.
    assert (await pg_session.execute(select(User.id))).all() == []


async def test_the_floor_holds_and_the_turn_completes_on_postgres(pg_session):
    await pg_session.execute(text("DROP TABLE active_symptom_states"))

    result = await handle_chat(
        pg_session, uuid.uuid4(), "should i be worried about this", FakeProvider()
    )
    assert result.risk_level == HIGH
    assert result.response_message


async def test_a_failed_profile_read_does_not_become_a_clinical_absence(pg_session):
    """The chain the audit described: the profile read fails, the aborted
    transaction makes the condition read fail too, and THAT rendered as
    "there are no conditions on your record"."""
    await _seed(pg_session)
    await pg_session.execute(text('ALTER TABLE "user" DROP COLUMN gender'))

    out = await handle_about_me_query(pg_session, USER, "who am i")
    assert out is not None
    low = out["reply"].lower()
    assert "type 2 diabetes" in low
    assert "no conditions on your record" not in low
    assert "could not read your profile" in low

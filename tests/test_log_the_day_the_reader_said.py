"""A backdated lifestyle entry lands on the day the reader said, or nowhere.

The tool schema lets the model send ``days_ago`` up to 30. The executor used
to turn that into "I had 3 coffee 3 days ago" for the free-text parser to
re-read, and that parser knew only "yesterday" and "day before yesterday" --
so ``days_ago=3`` was written to TODAY, confirmed as "for today", and echoed
back to the model as ``days_ago=3``. A reader backfilling a week got wrong
dates in their own tracker with nothing to tell them or the model.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.chat.abilities import parse_tracker_add
from app.chat.tools.registry import execute_tool
from app.coredata.service import utcnow
from app.llm.tools import ToolCall
from app.models.coredata import LifestyleLog


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments)


async def _rows(db_session, user_id):
    return (
        await db_session.execute(
            select(LifestyleLog).where(LifestyleLog.user_id == user_id)
        )
    ).scalars().all()


async def test_a_days_ago_the_schema_allows_is_written_to_that_day(db_session):
    user_id = uuid.uuid4()
    result = await execute_tool(
        db_session, user_id,
        _call("log_lifestyle_entry", kind="coffee", quantity=3, days_ago=3),
        None,
    )
    payload = json.loads(result.content)
    assert not result.is_error
    rows = await _rows(db_session, user_id)
    # Written to the day the reader said, or not written at all -- never today.
    if rows:
        assert len(rows) == 1
        assert rows[0].logged_at.date() == (utcnow() - timedelta(days=3)).date()
        assert "3 days ago" in payload["deterministic_reply"]
        assert "today" not in payload["deterministic_reply"]
    else:
        assert payload.get("ok") is False


async def test_a_days_ago_beyond_the_schema_is_refused_not_written_today(db_session):
    user_id = uuid.uuid4()
    result = await execute_tool(
        db_session, user_id,
        _call("log_lifestyle_entry", kind="water", quantity=2, days_ago=45),
        None,
    )
    payload = json.loads(result.content)
    assert not result.is_error
    assert payload.get("ok") is False
    assert "NOT saved" in payload["note"]
    assert await _rows(db_session, user_id) == []


async def test_the_schemas_own_word_for_smoking_is_logged(db_session):
    """The tool description says ``kind`` is one of "water, coffee, tea,
    alcohol, smoking". The parser knew "cigarettes" but not "smoking", so the
    tool's own documented value could not be logged at all."""
    user_id = uuid.uuid4()
    result = await execute_tool(
        db_session, user_id,
        _call("log_lifestyle_entry", kind="smoking", quantity=2),
        None,
    )
    payload = json.loads(result.content)
    assert not result.is_error
    rows = await _rows(db_session, user_id)
    assert [r.log_type for r in rows] == ["smoking"]
    assert "2 cigarettes" in payload["deterministic_reply"]


async def test_an_unknown_kind_is_refused_with_the_reason(db_session):
    user_id = uuid.uuid4()
    result = await execute_tool(
        db_session, user_id,
        _call("log_lifestyle_entry", kind="samosa", quantity=2),
        None,
    )
    payload = json.loads(result.content)
    assert payload.get("ok") is False
    assert "samosa" in payload["note"]
    assert await _rows(db_session, user_id) == []


def test_the_free_text_path_reads_n_days_ago_too():
    """Same harm on the typed path: "3 days ago" logged today."""
    offsets = {
        text: getattr(parse_tracker_add(text), "day_offset", None)
        for text in (
            "I had 2 cups of coffee 3 days ago",
            "I had 2 cups of coffee yesterday",
            "I had 2 cups of coffee",
        )
    }
    assert list(offsets.values()) == [3, 1, 0]

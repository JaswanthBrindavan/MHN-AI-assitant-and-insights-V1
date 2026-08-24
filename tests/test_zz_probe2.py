"""Probe 2: connection already established, then concurrent savepoints."""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text

from app.chat.tools.registry import execute_tool
from app.llm.tools import ToolCall


def _call(cid, name, **arguments):
    return ToolCall(id=cid, name=name, arguments=arguments)


async def test_warm_connection_parallel(db_session):
    uid = uuid.uuid4()
    # Warm the connection first, so begin_nested is not "provisioning".
    await db_session.execute(text("SELECT 1"))
    calls = [
        _call("a", "get_family_members"),
        _call("b", "get_health_summary", period="week"),
        _call("c", "log_lifestyle_entry", kind="water", quantity=3),
    ]
    results = await asyncio.gather(
        *(execute_tool(db_session, uid, c, None) for c in calls)
    )
    for r in results:
        print("RESULT", r.call_id, r.is_error, r.content[:100])
    try:
        print("SESSION OK:", (await db_session.execute(text("SELECT 1"))).scalar())
    except Exception as exc:  # noqa: BLE001
        print("SESSION BROKEN:", type(exc).__name__, exc)
    # what actually landed in the tracker?
    from app.models.coredata import LifestyleLog
    from sqlalchemy import select
    rows = (await db_session.execute(select(LifestyleLog))).scalars().all()
    print("LIFESTYLE ROWS:", len(rows))

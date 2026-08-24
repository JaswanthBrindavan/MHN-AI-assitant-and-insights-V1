from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text

from app.chat.tools.registry import EXECUTORS, execute_tool
from app.llm.tools import ToolCall


async def test_probe_concurrency_exception(db_session, monkeypatch, caplog):
    async def _slow(db, user_id, args, session_id):
        await db.execute(text("SELECT 1"))
        await asyncio.sleep(0.02)
        await db.execute(text("SELECT 1"))
        return {"deterministic_reply": "ok"}

    monkeypatch.setitem(EXECUTORS, "get_latest_metric", _slow)
    monkeypatch.setitem(EXECUTORS, "get_family_members", _slow)
    with caplog.at_level("WARNING"):
        await asyncio.gather(
            execute_tool(db_session, uuid.uuid4(),
                         ToolCall("c1", "get_latest_metric", {"metric": "x"}), None),
            execute_tool(db_session, uuid.uuid4(),
                         ToolCall("c2", "get_family_members", {}), None),
            return_exceptions=True,
        )
    for rec in caplog.records:
        print("\nPROBE3 LOG:", rec.getMessage())
        if rec.exc_info:
            import traceback
            print("PROBE3 EXC:", "".join(
                traceback.format_exception_only(rec.exc_info[0], rec.exc_info[1])))

"""Probe: concurrent tool execution on ONE AsyncSession (what agent.py does)."""
from __future__ import annotations

import asyncio
import json
import uuid

from sqlalchemy import text

from app.chat.tools.registry import execute_tool
from app.llm.tools import ToolCall


def _call(cid, name, **arguments):
    return ToolCall(id=cid, name=name, arguments=arguments)


async def test_parallel_tool_calls_share_one_session(db_session):
    from app.models.coredata import Report
    uid = uuid.uuid4()
    db_session.add(Report(id=902, user_id=uid, filepath="r/x", private=False,
        content={"ai": {"classification": {"section": "reports", "title": "L"},
                        "extraction": {"results": [
                            {"test_name": "HbA1c", "value": "6.1", "unit": "%",
                             "value_numeric": 6.1, "abnormal_flag": "high"}]}}}))
    await db_session.flush()

    calls = [
        _call("a", "get_report_parameter", parameter="HbA1c"),
        _call("b", "get_report_parameter", parameter="creatinine"),
        _call("c", "get_family_members"),
        _call("d", "get_health_summary", period="week"),
    ]
    results = await asyncio.gather(*(execute_tool(db_session, uid, c, None) for c in calls))
    for r in results:
        print("RESULT", r.call_id, r.is_error, r.content[:120])
    # session usable afterwards?
    try:
        print("SESSION OK:", (await db_session.execute(text("SELECT 1"))).scalar())
    except Exception as exc:  # noqa: BLE001
        print("SESSION BROKEN:", type(exc).__name__, exc)
        raise
    errs = [r for r in results if r.is_error]
    assert not errs, [json.loads(r.content) for r in errs]

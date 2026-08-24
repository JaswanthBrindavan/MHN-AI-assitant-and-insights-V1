"""TEMPORARY review probe 2 — delete after the review."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.chat.orchestrator import handle_chat
from app.chat.tools.registry import EXECUTORS, execute_tool
from app.llm.fake import FakeProvider
from app.llm.tools import LLMTurn, ToolCall


async def test_probe_concurrent_savepoints(db_session, monkeypatch):
    """Two tool calls whose DB work genuinely overlaps on ONE AsyncSession."""

    async def _slow(db, user_id, args, session_id):
        await db.execute(text("SELECT 1"))
        await asyncio.sleep(0.02)
        await db.execute(text("SELECT 1"))
        return {"deterministic_reply": "ok"}

    monkeypatch.setitem(EXECUTORS, "get_latest_metric", _slow)
    monkeypatch.setitem(EXECUTORS, "get_family_members", _slow)

    results = await asyncio.gather(
        execute_tool(db_session, uuid.uuid4(),
                     ToolCall("c1", "get_latest_metric", {"metric": "hba1c"}), None),
        execute_tool(db_session, uuid.uuid4(),
                     ToolCall("c2", "get_family_members", {}), None),
        return_exceptions=True,
    )
    print("\nPROBE-CONCURRENCY:", results)


async def test_probe_legacy_action_for_a_stated_bp(db_session, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("CHAT_ENGINE", "legacy")
    get_settings.cache_clear()
    provider = FakeProvider(responses=["x"])
    result = await handle_chat(
        db_session, uuid.uuid4(), "my bp is 195/125", provider
    )
    print("\nPROBE-LEGACY-ACTION:", result.recommended_action)
    print("PROBE-LEGACY-REPLY:", result.response_message[:200])
    print("PROBE-LEGACY-PROV:", result.provenance)
    get_settings.cache_clear()


@pytest.fixture
def _agentic(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("CHAT_ENGINE", "agentic")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_probe_documents_dropped(db_session, _agentic):
    from app.models.coredata import Report

    user_id = uuid.uuid4()
    db_session.add(Report(id=911, user_id=user_id, filepath="reports/y",
                          private=False,
                          content={"ai": {"classification":
                                          {"section": "reports", "title": "CBC"}}}))
    await db_session.flush()
    provider = FakeProvider(turns=[
        LLMTurn(tool_calls=(ToolCall("c1", "get_documents",
                                     {"kinds": ["report"]}),),
                stop_reason="tool_use"),
        LLMTurn(text="You have one report on file: CBC."),
    ])
    result = await handle_chat(db_session, user_id, "show me my reports", provider)
    print("\nPROBE-DOCS documents:", result.documents)
    print("PROBE-DOCS visual:", result.visual)
    print("PROBE-DOCS citations:", result.citations)
    print("PROBE-DOCS reply:", result.response_message)


async def test_probe_tool_executed_at_high_risk(db_session, _agentic):
    """Tools are NOT offered at HIGH risk — but does an unoffered call still run?"""
    provider = FakeProvider(turns=[
        LLMTurn(tool_calls=(ToolCall("c1", "log_lifestyle_entry",
                                     {"kind": "alcohol", "quantity": 3}),),
                stop_reason="tool_use"),
        LLMTurn(text="Please seek medical care promptly and get that checked."),
    ])
    result = await handle_chat(
        db_session, uuid.uuid4(), "I have severe chest pain", provider
    )
    print("\nPROBE-HIGH tools offered:", provider.calls[0]["tools"])
    print("PROBE-HIGH tools executed:", result.provenance.get("tools"))
    print("PROBE-HIGH risk:", result.risk_level)
    from sqlalchemy import select

    from app.models.coredata import LifestyleLog
    rows = (await db_session.execute(select(LifestyleLog))).scalars().all()
    print("PROBE-HIGH lifestyle rows written:", len(rows))

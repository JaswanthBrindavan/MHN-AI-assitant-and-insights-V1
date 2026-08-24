from __future__ import annotations
import asyncio, uuid
from sqlalchemy import text
from app.chat.agent import run_agent
from app.chat.tools.registry import EXECUTORS, execute_tool
from app.llm.fake import FakeProvider
from app.llm.tools import LLMTurn, ToolCall, ToolSpec, UserMessage

SPEC = ToolSpec("t", "d", {"type": "object", "properties": {}, "additionalProperties": False})


async def test_probe_gather_orphans(db_session):
    """One executor raises; do siblings keep running and touching db?"""
    still_running = []

    async def _executor(call):
        if call.id == "boom":
            await asyncio.sleep(0.01)
            raise TypeError("keys must be str (json.dumps escape)")
        try:
            await asyncio.sleep(0.05)
            await db_session.execute(text("SELECT 1"))
            still_running.append(call.id)
        except BaseException as e:      # noqa
            still_running.append(f"{call.id}:{type(e).__name__}")
            raise
        return None

    provider = FakeProvider(turns=[LLMTurn(
        tool_calls=(ToolCall("boom", "t", {}), ToolCall("sib", "t", {})),
        stop_reason="tool_use")])
    try:
        await run_agent(provider, "s", [UserMessage("x")], [SPEC], _executor)
    except TypeError as e:
        print("run_agent raised:", e)
    print("right after gather raised, siblings done:", still_running)
    # simulate the orchestrator continuing on the same session
    await db_session.execute(text("SELECT 1"))
    await asyncio.sleep(0.1)
    print("after orchestrator continued, siblings done:", still_running)
    assert still_running == [], f"orphaned tasks still touched the session: {still_running}"

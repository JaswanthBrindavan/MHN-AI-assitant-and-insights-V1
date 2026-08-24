"""Tool dispatch — SAVEPOINT-isolated, fail-closed to an error result.

A tool must NEVER raise into the agent loop. Two reasons:

* the model needs to SEE that a call failed so it can recover or say so, and
* a handler crash must roll back only its own writes — a missing core table in
  a standalone deployment must not poison the session for everything after it.

So every failure becomes a ToolResult with ``is_error=True`` and a short,
non-leaking message. Errors are logged without the arguments, which can carry
PHI.
"""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.tools import executors
from app.chat.tools.definitions import TOOL_SPECS
from app.llm.tools import ToolCall, ToolResult

logger = logging.getLogger("davi.tools")

EXECUTORS = {
    "get_latest_metric": executors.get_latest_metric,
    "get_report_parameter": executors.get_report_parameter,
    "get_documents": executors.get_documents,
    "check_value_against_range": executors.check_value_against_range,
    "log_lifestyle_entry": executors.log_lifestyle_entry,
    "get_health_summary": executors.get_health_summary,
    "get_family_members": executors.get_family_members,
    "get_condition_guidance": executors.get_condition_guidance,
    "lookup_medicine": executors.lookup_medicine,
}

__all__ = ["EXECUTORS", "TOOL_SPECS", "execute_tool"]


def _error(call_id: str, message: str) -> ToolResult:
    return ToolResult(
        call_id=call_id, content=json.dumps({"error": message}), is_error=True
    )


async def execute_tool(
    db: AsyncSession,
    user_id: uuid.UUID,
    call: ToolCall,
    session_id: uuid.UUID | None = None,
) -> ToolResult:
    """Run one tool call. Always returns a ToolResult — never raises."""
    fn = EXECUTORS.get(call.name)
    if fn is None:
        # A hallucinated tool name. Tell the model plainly so it stops.
        logger.warning("model requested unknown tool %r", call.name)
        return _error(
            call.id,
            f"No tool named {call.name!r} exists. Available tools: "
            + ", ".join(sorted(EXECUTORS)),
        )
    if not isinstance(call.arguments, dict):
        return _error(call.id, "Tool arguments could not be read.")

    try:
        # SAVEPOINT: a failure rolls back only this tool's writes.
        async with db.begin_nested():
            payload = await fn(db, user_id, call.arguments, session_id)

        if payload is None:
            # Not an error — "nothing on file" is a real, useful answer.
            return ToolResult(
                call_id=call.id,
                content=json.dumps(
                    {
                        "found": False,
                        "note": "Nothing on file for that. Say so plainly; "
                        "do not estimate a value.",
                    }
                ),
            )
        # Serialization is INSIDE the boundary on purpose. default=str covers
        # dates and UUIDs but not, say, a non-str dict key, and a TypeError
        # escaping here would propagate out of asyncio.gather and orphan the
        # sibling tool calls mid-flight.
        content = json.dumps(payload, default=str)
    except Exception:  # noqa: BLE001 — a tool must never break the loop
        # Deliberately no arguments in the log line: they can carry PHI.
        logger.warning("tool %s failed", call.name, exc_info=True)
        return _error(call.id, "That lookup could not be completed.")

    return ToolResult(call_id=call.id, content=content)

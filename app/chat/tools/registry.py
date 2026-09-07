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

from app.chat.abilities import (
    _POSSESSIVE_NAME_RE,
    _POSSESSIVE_STOP,
    find_relation,
    names_another_person,
)
from app.chat.tools import executors
from app.chat.tools.definitions import TOOL_SPECS
from app.coredata.service import (
    resolve_family_member,
    resolve_family_member_by_name,
)
from app.llm.tools import ToolCall, ToolResult
from app.telemetry import record_fail_open, tool_calls

logger = logging.getLogger("davi.tools")

EXECUTORS = {
    "get_latest_metric": executors.get_latest_metric,
    "get_report_parameter": executors.get_report_parameter,
    "get_documents": executors.get_documents,
    "check_value_against_range": executors.check_value_against_range,
    "log_lifestyle_entry": executors.log_lifestyle_entry,
    "get_health_summary": executors.get_health_summary,
    "get_tracker_total": executors.get_tracker_total,
    "get_trends_and_patterns": executors.get_trends_and_patterns,
    "get_family_members": executors.get_family_members,
    "get_family_member_record": executors.get_family_member_record,
    "get_condition_guidance": executors.get_condition_guidance,
    "lookup_medicine": executors.lookup_medicine,
    "get_document_ai_result": executors.get_document_ai_result,
    "get_section_details": executors.get_section_details,
    "get_doctor_consults": executors.get_doctor_consults,
    "list_medications": executors.list_medications,
    "get_medication_adherence": executors.get_medication_adherence,
    "add_medication": executors.add_medication,
    "stop_medication": executors.stop_medication,
    "remove_medication": executors.remove_medication,
    "analyze_image": executors.analyze_image,
}

#: Tools that answer ONLY from the reader's own rows. Each one's handler
#: filters hard on ``user_id``, and there is no family-scoped read behind any
#: of them: the only reads in ``app/coredata/service.py`` that take a
#: ``viewer_id`` are ``latest_documents``, ``can_view_document`` and
#: ``shared_report_contents`` (plus ``medical_records(shared_only=True)``),
#: and none of these tools use them. Asked about a relative these return the
#: READER's figure, and the model then presents it as the relative's.
#:
#: ``get_documents``, ``get_document_ai_result`` and
#: ``get_family_member_record`` are deliberately absent: their handlers
#: resolve a relation or a named member and read under the sharing gate,
#: which is the right answer to the same question.
READER_ONLY_TOOLS = frozenset({
    "get_latest_metric",
    "get_report_parameter",
    "get_section_details",
    "get_tracker_total",
    "get_health_summary",
})


async def _about_someone_else(
    db: AsyncSession, user_id: uuid.UUID, asked: str
) -> dict:
    """The result for a reader-only tool asked about somebody else.

    Not ``None``: the registry turns that into "Nothing on file for that",
    which asserts the reader has no such data — false, and a different wrong
    answer to the same question.

    The relation is resolved rather than merely refused, because what MHN can
    offer about a family member is real and specific: their DOCUMENTS, under
    the sharing settings they chose. Naming who was understood, and whether
    they are connected, is the difference between a dead end and a next step.
    """
    relation = find_relation(asked)
    who = relation
    member: uuid.UUID | None = None
    if relation is not None:
        member = await resolve_family_member(db, user_id, relation)
    else:
        m = _POSSESSIVE_NAME_RE.search(asked.lower())
        if m and m.group(1) not in _POSSESSIVE_STOP:
            who = m.group(1)
            member = await resolve_family_member_by_name(db, user_id, who)

    subject = f"the reader's {relation}" if relation else (
        f"{who}" if who else "someone other than the reader"
    )
    if member is not None:
        nxt = (
            "They ARE connected in Family Connect and sharing. Their shared "
            "lab values and conditions come from get_family_member_record "
            "and their documents from get_documents — call one of those "
            "instead. Vitals, trackers and section fields are not shared for "
            "anyone but the reader, so those you cannot show."
        )
    elif who:
        nxt = (
            "Nobody matching that is connected and sharing, so there is "
            "nothing of theirs to read. Say so plainly and mention Family "
            "Connect, rather than implying the data does not exist."
        )
    else:
        nxt = (
            "Say you can only show the reader's own readings here."
        )

    return {
        "found": False,
        "about": subject,
        "note": (
            f"This question is about {subject}, and this tool reads only the "
            f"reader's own records. Do NOT answer it from them, and do NOT "
            f"present the reader's own figure as theirs. This is NOT a "
            f"statement that anyone has no data. {nxt}"
        ),
    }


# Tools that MUTATE state: their None is a failed action, never an empty read.
WRITE_TOOLS = frozenset({
    "add_medication", "stop_medication", "remove_medication",
    "log_lifestyle_entry",
})

# Tools whose output a MODEL produced rather than a database returned. Their
# values must never become sources for the numeric-fidelity guard: an OCR
# misread would otherwise be authorised by the one guard designed to catch it.
UNTRUSTED_VALUE_TOOLS = frozenset({"analyze_image"})

__all__ = [
    "EXECUTORS",
    "TOOL_SPECS",
    "UNTRUSTED_VALUE_TOOLS",
    "execute_tool",
]


def _error(call_id: str, message: str, tool: str = "unknown") -> ToolResult:
    tool_calls.inc(tool=tool, outcome="error")
    return ToolResult(
        call_id=call_id, content=json.dumps({"error": message}), is_error=True
    )


async def execute_tool(
    db: AsyncSession,
    user_id: uuid.UUID,
    call: ToolCall,
    session_id: uuid.UUID | None = None,
    asked: str | None = None,
    visuals: list[dict] | None = None,
    sources: list | None = None,
    documents: list[dict] | None = None,
) -> ToolResult:
    """Run one tool call. Always returns a ToolResult — never raises.

    ``asked`` is the reader's own message, not the model's reconstruction of
    it. Every executor here builds a first-person sentence out of the model's
    structured arguments — ``get_latest_metric`` sends "what is my latest
    blood pressure" — so by the time a parser sees it, "my mother's" is gone
    and the guard in ``abilities.names_another_person`` cannot fire. Passing
    the real message is what lets it.

    ``visuals`` collects chart payloads OUT OF BAND: a rendered SVG is prompt
    the model cannot use, but the client still needs it, so the caller passes a
    list and puts what lands in it on the ChatResult.

    ``sources`` collects the corpus chunks a tool RENDERED, the same way —
    they are what the caller must cite, and the model has the rendered text
    already.

    ``documents`` collects the document cards, for the same reason again: the
    client needs them to render an open button, the model cannot use them, and
    the reply already names the titles.
    """
    fn = EXECUTORS.get(call.name)
    if fn is None:
        # A hallucinated tool name. Tell the model plainly so it stops.
        logger.warning("model requested unknown tool %r", call.name)
        return _error(
            call.id,
            f"No tool named {call.name!r} exists. Available tools: "
            + ", ".join(sorted(EXECUTORS)),
            tool="unknown",
        )
    if not isinstance(call.arguments, dict):
        return _error(call.id, "Tool arguments could not be read.", tool=call.name)

    # A relative's question, answered from the reader's rows (audit H7).
    #
    # The deterministic parsers have refused this since #72, but that guard
    # reads the MESSAGE, and on this path no executor ever sees one — each
    # rebuilds a first-person question from the model's arguments. So the
    # refusal held on the legacy engine and was bypassed entirely on the
    # agentic one, which is what production runs.
    #
    # Enforced here rather than in the prompt for the same reason the wearable
    # no-grade rule is: a rule the model may decline to follow is not a guard.
    if call.name in READER_ONLY_TOOLS and asked and names_another_person(asked):
        logger.info("tool %s declined: the turn is about another person", call.name)
        return ToolResult(
            call_id=call.id,
            content=json.dumps(await _about_someone_else(db, user_id, asked)),
        )

    try:
        # SAVEPOINT: a failure rolls back only this tool's writes.
        async with db.begin_nested():
            payload = await fn(db, user_id, call.arguments, session_id)

        if payload is None:
            if call.name in WRITE_TOOLS:
                # A WRITE that produced nothing is a failure to act, not an
                # empty read — the old read-shaped note coached the model to
                # misreport a failed write as "nothing on file".
                return ToolResult(
                    call_id=call.id,
                    content=json.dumps(
                        {
                            "ok": False,
                            "note": "The update could not be made. Tell the "
                            "reader plainly it was NOT saved; never imply it "
                            "was.",
                        }
                    ),
                )
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
        visual = payload.pop(executors.OUT_OF_BAND_VISUAL, None)
        if visual is not None and visuals is not None:
            visuals.append(visual)
        if visual is not None:
            # Say a chart exists, without handing over the numbers in it.
            #
            # The values are lifted out of band so the model cannot quote a
            # figure it never read — but the model then had no idea a chart had
            # been produced, and answered "I can't generate a graph, but here's
            # what's on file" **underneath the graph it had just produced**.
            # The reader saw a chart and a sentence denying it in the same reply.
            #
            # The title and the count are enough to stop that, and both are
            # already in the deterministic reply, so nothing new becomes
            # quotable and the fidelity guard has the same sources it had.
            payload["chart_shown_to_reader"] = {
                "title": visual.get("title"),
                "points": len(visual.get("values") or []),
                "note": (
                    "This chart is displayed to the reader with your answer. "
                    "Do not say you cannot draw or plot one, and do not "
                    "describe the individual points — they can see it."
                ),
            }
        used = payload.pop(executors.OUT_OF_BAND_SOURCES, None)
        if used and sources is not None:
            sources.extend(used)
        cards = payload.pop(executors.OUT_OF_BAND_DOCUMENTS, None)
        if cards and documents is not None:
            documents.extend(cards)
        # Serialization is INSIDE the boundary on purpose. default=str covers
        # dates and UUIDs but not, say, a non-str dict key, and a TypeError
        # escaping here would propagate out of asyncio.gather and orphan the
        # sibling tool calls mid-flight.
        # ensure_ascii=False deliberately: the default escapes the en dash in a
        # reference range to –, and the digits 2013 then sit immediately
        # before the figure, so the fidelity guard's (?<!\d) lookbehind refused
        # to trace a value that was verbatim correct. Three ordinary value-check
        # questions degraded to the safe reply because of an escape sequence.
        content = json.dumps(payload, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — a tool must never break the loop
        # Deliberately no arguments in the log line: they can carry PHI.
        logger.warning("tool %s failed", call.name, exc_info=True)
        record_fail_open("tools")
        return _error(call.id, "That lookup could not be completed.", tool=call.name)

    tool_calls.inc(tool=call.name, outcome="ok")
    return ToolResult(
        call_id=call.id,
        content=content,
        trusted_values=call.name not in UNTRUSTED_VALUE_TOOLS,
    )

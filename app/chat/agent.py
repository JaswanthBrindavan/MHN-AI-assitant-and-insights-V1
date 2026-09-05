"""The bounded agentic loop.

The model may call tools, read their results, and call more — up to
``max_rounds``. After that one final call is made with NO tools offered, which
forces a text answer instead of an unbounded loop. A loop that cannot terminate
is not an acceptable failure mode in a patient-facing path.

This module owns control flow ONLY. Every safety property — the triage floor,
output validation, grounding, numeric fidelity — lives in the orchestrator,
before and after this runs. Keep it that way: safety that is spread across a
loop is safety nobody can audit.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from app.llm.tools import (
    AssistantMessage,
    LLMTurn,
    Message,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    ToolSpec,
)

logger = logging.getLogger("davi.agent")

DEFAULT_MAX_ROUNDS = 3

_FORCE_ANSWER = (
    "\n\nYou have used your tool budget for this turn. Answer now, in plain "
    "language, using only what you already have. If something is still "
    "missing, say so plainly and suggest what the reader can check with their "
    "clinician. Do not describe the tools or the budget."
)

Executor = Callable[[ToolCall], Awaitable[ToolResult]]


@dataclass
class AgentOutcome:
    """What the loop produced, plus everything the caller needs to audit it."""

    text: str = ""
    rounds: int = 0
    tool_names: list[str] = field(default_factory=list)
    # Raw tool payloads — the numeric-fidelity guard checks the reply's stated
    # values against these.
    source_texts: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    # True when the budget ran out and the answer came from the forced call.
    forced: bool = False
    stop_reason: str = "end_turn"

    @property
    def used_tools(self) -> bool:
        return bool(self.tool_names)


def _accumulate_usage(total: dict, turn: LLMTurn) -> None:
    if not turn.usage:
        return
    for key, value in turn.usage.items():
        total[key] = total.get(key, 0) + value
    total["calls"] = total.get("calls", 0) + 1


def _as_error(call: ToolCall, exc: BaseException) -> ToolResult:
    """Turn an executor exception into a result the model can act on.

    An executor is contracted never to raise; if one does anyway, the model is
    told that call failed rather than the whole turn dying.
    """
    logger.warning(
        "tool executor raised for %s: %s", call.name, type(exc).__name__
    )
    return ToolResult(
        call_id=call.id,
        content='{"error": "That lookup could not be completed."}',
        is_error=True,
    )


def append_directive(system: str | Sequence[str], extra: str) -> str | list[str]:
    """Add a per-turn instruction WITHOUT disturbing the cached prefix.

    A split system prompt keeps its byte-identical prefix in element 0; the
    extra goes on the volatile tail. Appending it to element 0 instead would
    change the one string that must never change, turning every corrected or
    budget-exhausted turn into a cache miss for everything after it.
    """
    if isinstance(system, str):
        return system + extra
    parts = list(system)
    if len(parts) < 2:
        # One element (or none) means there is no volatile tail to append to,
        # and writing into parts[0] is precisely what this function exists to
        # prevent. Fall back to a plain string: no breakpoint, no silent
        # corruption of the cached prefix.
        return (parts[0] + extra) if parts else extra.lstrip("\n")
    parts[-1] = (parts[-1] + extra) if parts[-1] else extra.lstrip("\n")
    return parts


async def run_agent(
    provider,
    system: str | Sequence[str],
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    executor: Executor,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> AgentOutcome:
    """Drive the tool loop to a text answer.

    Raises only what the provider raises — the caller treats that as a
    guardrail failure and degrades to a safe reply.
    """
    history: list[Message] = list(messages)
    outcome = AgentOutcome()

    for round_index in range(max_rounds):
        turn = await provider.generate_turn(
            system=system, messages=history, tools=tools
        )
        _accumulate_usage(outcome.usage, turn)

        if not turn.wants_tools:
            outcome.text = turn.text
            outcome.rounds = round_index
            outcome.stop_reason = turn.stop_reason
            outcome.messages = history
            return outcome

        history.append(
            AssistantMessage(content=turn.text, tool_calls=turn.tool_calls)
        )
        # SEQUENTIAL, deliberately. Every executor shares ONE AsyncSession,
        # and SQLAlchemy does not support concurrent operations on one — a
        # gather here is not merely pointless (one session is one connection,
        # so the work serialises anyway) but actively destructive: measured,
        # only the FIRST of four calls succeeded and the rest came back
        # "could not be completed" on perfectly good data. The model would
        # then tell the reader their records are unavailable when they are not.
        #
        # Results still travel together in ONE message — that is about the
        # wire shape, not the execution order, and splitting them teaches the
        # model to stop calling tools in parallel.
        # Rounds are bounded but the CALLS per round were not — one model
        # turn could queue unbounded sequential DB/HTTP work. Everything past
        # the cap is answered with an error result the model can see.
        MAX_CALLS_PER_ROUND = 8
        overflow = list(turn.tool_calls[MAX_CALLS_PER_ROUND:])
        turn_calls = list(turn.tool_calls[:MAX_CALLS_PER_ROUND])
        results = []
        for call in overflow:
            results.append(_as_error(
                call,
                RuntimeError(
                    f"too many tool calls in one round (max "
                    f"{MAX_CALLS_PER_ROUND}); call fewer tools at once"
                ),
            ))
        for call in turn_calls:
            try:
                results.append(await executor(call))
            except Exception as exc:  # noqa: BLE001 — executors must not kill a turn
                results.append(_as_error(call, exc))
        history.append(ToolResultMessage(results=tuple(results)))
        outcome.tool_names.extend(call.name for call in turn.tool_calls)
        # ONLY trusted results become numeric sources. A model-produced
        # result (a vision transcription) is a claim, not a record, and
        # letting it in would have the fidelity guard authorise a misread.
        outcome.source_texts.extend(
            r.content for r in results if r.trusted_values
        )

    # Budget exhausted. Offering no tools is what forces text — a model that
    # keeps asking would otherwise never produce an answer.
    logger.info(
        "agent tool budget exhausted after %d rounds (tools: %s)",
        max_rounds,
        ", ".join(sorted(set(outcome.tool_names))),
    )
    final = await provider.generate_turn(
        system=append_directive(system, _FORCE_ANSWER),
        messages=history,
        tools=(),
    )
    _accumulate_usage(outcome.usage, final)
    outcome.text = final.text
    outcome.rounds = max_rounds
    outcome.stop_reason = final.stop_reason
    outcome.forced = True
    outcome.messages = history
    return outcome


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #
RECOVERY_FAILED = (
    "I'm not able to answer that one safely enough to be useful, and I'd "
    "rather say so than guess. A clinician can look at this properly with "
    "you. Is there something else I can help with, or would you like me to "
    "try explaining it a different way?"
)

# Why each guard rejected, phrased as an instruction the model can act on.
_CORRECTIONS = {
    "banned:diagnostic-assertion": (
        "Your previous answer stated or implied that the reader HAS a "
        "condition. Rewrite it as what the information suggests is worth "
        "discussing with their doctor. Assert no diagnosis."
    ),
    "banned:provider-leak": (
        "Your previous answer named an AI model, provider or company. You are "
        "Ink, the reader's personal health assistant. Rewrite without naming "
        "any of them."
    ),
    "banned:numeric-disease-probability": (
        "Your previous answer gave a disease probability as a number. Rewrite "
        "it without any numeric likelihood."
    ),
    "missing-escalation": (
        "Your previous answer concerns a potentially serious symptom and must "
        "tell the reader to seek medical care promptly. Rewrite it so that "
        "instruction is unmistakable."
    ),
    "fidelity": (
        "Your previous answer stated a value that does not appear in the "
        "reader's records: {detail}. Use only values the tools returned, "
        "quoted exactly, or leave the number out. Do not COMPUTE a figure "
        "either — no averages, no per-day or per-night breakdowns, no unit "
        "conversions. Report the total the tool gave you and stop."
    ),
    "ungrounded_value": (
        "Your previous answer stated a dose or measurement with nothing to "
        "support it: {detail}. Remove the number, or say plainly that you do "
        "not have that figure."
    ),
}

_GENERIC_CORRECTION = (
    "Your previous answer did not pass this service's safety checks. Rewrite "
    "it more carefully, stay general, and route anything specific to a "
    "clinician."
)


def correction_for(reason: str, detail: str = "") -> str:
    """The corrective instruction for a rejection reason."""
    template = _CORRECTIONS.get(reason)
    if template is None:
        # Match on the family when the exact reason is unknown
        # (banned:<phrase> covers an open set of phrases).
        for key, value in _CORRECTIONS.items():
            if reason.startswith(key.split(":")[0] + ":"):
                template = value
                break
    if template is None:
        return _GENERIC_CORRECTION
    return template.format(detail=detail or "that value")


async def recover(
    provider,
    system: str | Sequence[str],
    messages: Sequence[Message],
    reason: str,
    detail: str = "",
) -> str | None:
    """One corrective retry. Returns the rewritten text, or None.

    None means "fall back to the safe reply" — the existing floor is unchanged,
    just reached less often. Exactly one extra call, never a loop: a model that
    keeps failing the guards must not be allowed to keep spending the reader's
    time.
    """
    correction = correction_for(reason, detail)
    if isinstance(system, str):
        corrected: str | list[str] = system + "\n\n" + correction
    else:
        # Append to the VOLATILE tail, never the cached prefix. Appending to
        # element 0 would change the one string that must stay byte-identical
        # and silently turn every recovery into a cache miss for the turns
        # that follow.
        parts = list(system)
        parts[-1] = (parts[-1] + "\n\n" + correction) if parts[-1] else correction
        corrected = parts

    try:
        turn = await provider.generate_turn(
            system=corrected,
            messages=messages,
            tools=(),
        )
    except Exception:  # noqa: BLE001 — recovery must never make things worse
        logger.warning("recovery attempt failed", exc_info=True)
        return None
    return turn.text or None

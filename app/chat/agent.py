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

import asyncio
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


async def run_agent(
    provider,
    system: str,
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
        # Concurrent execution, then ALL results in one message — splitting
        # them teaches the model to stop calling tools in parallel.
        results = await asyncio.gather(
            *(executor(call) for call in turn.tool_calls)
        )
        history.append(ToolResultMessage(results=tuple(results)))
        outcome.tool_names.extend(call.name for call in turn.tool_calls)
        outcome.source_texts.extend(r.content for r in results)

    # Budget exhausted. Offering no tools is what forces text — a model that
    # keeps asking would otherwise never produce an answer.
    logger.info(
        "agent tool budget exhausted after %d rounds (tools: %s)",
        max_rounds,
        ", ".join(sorted(set(outcome.tool_names))),
    )
    final = await provider.generate_turn(
        system=system + _FORCE_ANSWER, messages=history, tools=()
    )
    _accumulate_usage(outcome.usage, final)
    outcome.text = final.text
    outcome.rounds = max_rounds
    outcome.stop_reason = final.stop_reason
    outcome.forced = True
    outcome.messages = history
    return outcome

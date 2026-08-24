"""The agent loop — bounded, parallel, and always terminating in text.

The termination property is the one that matters. A loop that can keep asking
for tools forever is not an acceptable failure mode on a patient-facing path.
"""

from __future__ import annotations

import asyncio

from app.chat.agent import run_agent
from app.llm.fake import FakeProvider
from app.llm.tools import (
    AssistantMessage,
    LLMTurn,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)

SPEC = ToolSpec(
    name="t",
    description="d",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)


async def _echo(call: ToolCall) -> ToolResult:
    return ToolResult(call_id=call.id, content='{"value": 6.1}')


def _tool_turn(*ids: str) -> LLMTurn:
    return LLMTurn(
        tool_calls=tuple(ToolCall(id=i, name="t", arguments={}) for i in ids),
        stop_reason="tool_use",
    )


# --------------------------------------------------------------------------- #
# Basic flow
# --------------------------------------------------------------------------- #
async def test_a_plain_answer_uses_no_rounds():
    provider = FakeProvider(turns=[LLMTurn(text="hello", stop_reason="end_turn")])
    out = await run_agent(provider, "sys", [UserMessage("hi")], [SPEC], _echo)
    assert out.text == "hello"
    assert out.rounds == 0
    assert out.tool_names == []
    assert not out.used_tools
    assert not out.forced


async def test_one_tool_round_then_an_answer():
    provider = FakeProvider(
        turns=[_tool_turn("c1"), LLMTurn(text="Your HbA1c was 6.1%.")]
    )
    out = await run_agent(provider, "sys", [UserMessage("hba1c?")], [SPEC], _echo)
    assert out.rounds == 1
    assert out.tool_names == ["t"]
    assert out.source_texts == ['{"value": 6.1}']
    assert out.used_tools


async def test_several_rounds_accumulate_sources_in_order():
    provider = FakeProvider(
        turns=[_tool_turn("c1"), _tool_turn("c2"), LLMTurn(text="done")]
    )
    out = await run_agent(provider, "sys", [UserMessage("x")], [SPEC], _echo)
    assert out.rounds == 2
    assert len(out.source_texts) == 2


# --------------------------------------------------------------------------- #
# Parallel execution
# --------------------------------------------------------------------------- #
async def test_parallel_calls_return_in_one_message():
    provider = FakeProvider(
        turns=[_tool_turn("c1", "c2"), LLMTurn(text="both done")]
    )
    out = await run_agent(provider, "sys", [UserMessage("x")], [SPEC], _echo)
    assert out.rounds == 1
    assert len(out.source_texts) == 2
    # The history must carry ONE ToolResultMessage holding both results.
    result_msgs = [m for m in out.messages if isinstance(m, ToolResultMessage)]
    assert len(result_msgs) == 1
    assert len(result_msgs[0].results) == 2


async def test_parallel_calls_actually_run_concurrently():
    order: list[str] = []

    async def _slow_then_fast(call: ToolCall) -> ToolResult:
        # c1 sleeps longer; if execution were serial it would finish first.
        await asyncio.sleep(0.05 if call.id == "c1" else 0.01)
        order.append(call.id)
        return ToolResult(call_id=call.id, content="{}")

    provider = FakeProvider(turns=[_tool_turn("c1", "c2"), LLMTurn(text="ok")])
    await run_agent(provider, "sys", [UserMessage("x")], [SPEC], _slow_then_fast)
    assert order == ["c2", "c1"], "tool calls did not run concurrently"


async def test_results_keep_call_order_even_when_they_finish_out_of_order():
    """asyncio.gather preserves input order — results must line up with the
    calls that produced them, not with completion time."""

    async def _reversed_timing(call: ToolCall) -> ToolResult:
        await asyncio.sleep(0.05 if call.id == "c1" else 0.01)
        return ToolResult(call_id=call.id, content=f'{{"id": "{call.id}"}}')

    provider = FakeProvider(turns=[_tool_turn("c1", "c2"), LLMTurn(text="ok")])
    out = await run_agent(
        provider, "sys", [UserMessage("x")], [SPEC], _reversed_timing
    )
    result_msgs = [m for m in out.messages if isinstance(m, ToolResultMessage)]
    assert [r.call_id for r in result_msgs[0].results] == ["c1", "c2"]


# --------------------------------------------------------------------------- #
# Termination
# --------------------------------------------------------------------------- #
async def test_budget_exhaustion_forces_a_final_text_answer():
    """A model that keeps asking for tools must still produce text."""
    provider = FakeProvider(
        turns=[_tool_turn("c1"), _tool_turn("c2"), LLMTurn(text="final")]
    )
    out = await run_agent(
        provider, "sys", [UserMessage("x")], [SPEC], _echo, max_rounds=2
    )
    assert out.rounds == 2
    assert out.text == "final"
    assert out.forced
    # The forced call offers NO tools — that is what makes it terminate.
    assert provider.calls[-1]["tools"] == []
    assert "tool budget" in provider.calls[-1]["system"]


async def test_the_forced_call_still_happens_if_the_model_asks_again():
    """Even the final call returning tool_use must not loop."""
    provider = FakeProvider(turns=[_tool_turn("c1"), _tool_turn("c2")])
    out = await run_agent(
        provider, "sys", [UserMessage("x")], [SPEC], _echo, max_rounds=1
    )
    assert out.forced
    assert out.rounds == 1


async def test_zero_rounds_goes_straight_to_the_forced_answer():
    provider = FakeProvider(turns=[LLMTurn(text="no tools for you")])
    out = await run_agent(
        provider, "sys", [UserMessage("x")], [SPEC], _echo, max_rounds=0
    )
    assert out.text == "no tools for you"
    assert out.forced


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #
async def test_a_failing_tool_does_not_stop_the_loop():
    async def _broken(call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.id, content='{"error": "nope"}', is_error=True)

    provider = FakeProvider(
        turns=[_tool_turn("c1"), LLMTurn(text="I could not look that up.")]
    )
    out = await run_agent(provider, "sys", [UserMessage("x")], [SPEC], _broken)
    assert out.text == "I could not look that up."
    assert out.source_texts == ['{"error": "nope"}']


async def test_a_refusal_stop_reason_is_surfaced_not_swallowed():
    provider = FakeProvider(turns=[LLMTurn(text="", stop_reason="refusal")])
    out = await run_agent(provider, "sys", [UserMessage("x")], [SPEC], _echo)
    assert out.stop_reason == "refusal"


async def test_provider_failure_propagates_to_the_caller():
    """The orchestrator owns fail-open; the loop must not silently swallow."""
    provider = FakeProvider(raises=RuntimeError("provider down"))
    try:
        await run_agent(provider, "sys", [UserMessage("x")], [SPEC], _echo)
    except RuntimeError as exc:
        assert "provider down" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("provider failure was swallowed")


# --------------------------------------------------------------------------- #
# History fidelity
# --------------------------------------------------------------------------- #
async def test_the_caller_history_is_not_mutated():
    """run_agent copies its input — the caller's list must be untouched."""
    provider = FakeProvider(turns=[_tool_turn("c1"), LLMTurn(text="ok")])
    original = [UserMessage("x")]
    await run_agent(provider, "sys", original, [SPEC], _echo)
    assert original == [UserMessage("x")]


async def test_history_alternates_assistant_then_results():
    provider = FakeProvider(turns=[_tool_turn("c1"), LLMTurn(text="ok")])
    out = await run_agent(provider, "sys", [UserMessage("x")], [SPEC], _echo)
    kinds = [type(m).__name__ for m in out.messages]
    assert kinds == ["UserMessage", "AssistantMessage", "ToolResultMessage"]


async def test_usage_accumulates_across_rounds():
    provider = FakeProvider(
        turns=[
            LLMTurn(
                tool_calls=(ToolCall("c1", "t", {}),),
                stop_reason="tool_use",
                usage={"input_tokens": 10, "output_tokens": 5},
            ),
            LLMTurn(text="ok", usage={"input_tokens": 20, "output_tokens": 7}),
        ]
    )
    out = await run_agent(provider, "sys", [UserMessage("x")], [SPEC], _echo)
    assert out.usage == {"input_tokens": 30, "output_tokens": 12, "calls": 2}


async def test_assistant_preamble_text_is_preserved_in_history():
    """The model often says 'let me check' before a call; dropping it loses
    context for the next round."""
    provider = FakeProvider(
        turns=[
            LLMTurn(
                text="let me check",
                tool_calls=(ToolCall("c1", "t", {}),),
                stop_reason="tool_use",
            ),
            LLMTurn(text="ok"),
        ]
    )
    out = await run_agent(provider, "sys", [UserMessage("x")], [SPEC], _echo)
    assistant = [m for m in out.messages if isinstance(m, AssistantMessage)][0]
    assert assistant.content == "let me check"

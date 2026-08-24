"""The internal tool vocabulary — provider-neutral by construction."""

from __future__ import annotations

import dataclasses

import pytest

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
    name="get_latest_metric",
    description="Fetch the reader's most recent value for a health metric.",
    input_schema={
        "type": "object",
        "properties": {"metric": {"type": "string"}},
        "required": ["metric"],
        "additionalProperties": False,
    },
)


def test_tool_types_are_frozen():
    # Construct locally: mutating the module-level SPEC would leak a corrupted
    # name into every later test if frozen ever regressed, and the resulting
    # failure would point at the fake's call log instead of at this bug.
    spec = ToolSpec(name="t", description="d", input_schema={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "other"  # type: ignore[misc]


def test_tool_result_defaults_to_success():
    assert ToolResult(call_id="c1", content='{"value": 6.1}').is_error is False


def test_tool_result_message_holds_all_results_together():
    """Parallel results must travel in ONE message — splitting them teaches
    the model to stop making parallel calls."""
    msg = ToolResultMessage(
        results=(
            ToolResult(call_id="c1", content="{}"),
            ToolResult(call_id="c2", content="{}"),
        )
    )
    assert tuple(r.call_id for r in msg.results) == ("c1", "c2")


def test_wants_tools_needs_both_the_stop_reason_and_the_calls():
    call = ToolCall(id="c1", name="t", arguments={})
    assert LLMTurn(tool_calls=(call,), stop_reason="tool_use").wants_tools
    assert not LLMTurn(text="hi").wants_tools
    # A truncated tool turn with no calls must NOT loop.
    assert not LLMTurn(stop_reason="tool_use").wants_tools
    # ...and calls present with a non-tool_use stop_reason must NOT loop
    # either. Without these two, `return bool(self.tool_calls)` passes — and
    # this is the sole loop condition for the agent in Task 6.
    assert not LLMTurn(tool_calls=(call,), stop_reason="max_tokens").wants_tools
    assert not LLMTurn(tool_calls=(call,), stop_reason="refusal").wants_tools


async def test_fake_provider_scripts_a_tool_call_then_an_answer():
    provider = FakeProvider(
        turns=[
            LLMTurn(
                text="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="get_latest_metric",
                        arguments={"metric": "hba1c"},
                    ),
                ),
                stop_reason="tool_use",
            ),
            LLMTurn(text="Your last HbA1c was 6.1%.", stop_reason="end_turn"),
        ]
    )

    first = await provider.generate_turn(
        system="sys", messages=[UserMessage("what was my hba1c")], tools=[SPEC]
    )
    assert first.stop_reason == "tool_use"
    assert first.tool_calls[0].name == "get_latest_metric"
    assert first.tool_calls[0].arguments == {"metric": "hba1c"}

    second = await provider.generate_turn(
        system="sys",
        messages=[
            UserMessage("what was my hba1c"),
            AssistantMessage(content="", tool_calls=first.tool_calls),
            ToolResultMessage(
                results=(ToolResult(call_id="c1", content='{"value": 6.1}'),)
            ),
        ],
        tools=[SPEC],
    )
    assert second.stop_reason == "end_turn"
    assert "6.1" in second.text

    # One shared call log; the tool names offered are recorded per call.
    assert [c["tools"] for c in provider.calls] == [
        ["get_latest_metric"],
        ["get_latest_metric"],
    ]


async def test_an_unscripted_turn_falls_back_instead_of_raising():
    provider = FakeProvider()
    turn = await provider.generate_turn(system="s", messages=[UserMessage("u")])
    assert turn.text == FakeProvider.DEFAULT
    assert turn.stop_reason == "end_turn"
    assert not turn.wants_tools
    assert provider.calls[-1]["tools"] == []


async def test_the_legacy_text_path_is_unchanged():
    """The text path, its script, and its default must not move.

    The DEFAULT text is load-bearing — it deliberately contains no "clinician",
    which is how evals/scenarios.json and the orchestrator tests tell a degraded
    safe reply apart from an ordinary model answer.
    """
    provider = FakeProvider(responses=["a plain answer"])
    assert await provider.generate(system="s", user="u") == "a plain answer"
    # Script exhausted → the default, not IndexError.
    assert await provider.generate(system="s", user="u2") == FakeProvider.DEFAULT
    assert "clinician" not in FakeProvider.DEFAULT
    assert "steady habits" in FakeProvider.DEFAULT
    assert provider.calls == [
        {"system": "s", "user": "u"},
        {"system": "s", "user": "u2"},
    ]


async def test_raises_records_the_call_before_failing():
    """`provider.calls == []` must mean the model was never reached — so an
    outage still records that it WAS reached."""
    provider = FakeProvider(raises=RuntimeError("provider down"))

    with pytest.raises(RuntimeError, match="provider down"):
        await provider.generate(system="s", user="u")
    with pytest.raises(RuntimeError, match="provider down"):
        await provider.generate_turn(system="s", messages=[UserMessage("u")])

    assert len(provider.calls) == 2


async def test_the_logged_messages_are_a_snapshot_not_an_alias():
    """`list(messages)` in generate_turn is load-bearing, not a redundant copy.

    The agent loop grows ONE list across rounds; without the copy every logged
    entry would alias the final state and the call log would be useless for
    reconstructing what the model actually saw on each round.
    """
    provider = FakeProvider()
    history = [UserMessage("first")]

    await provider.generate_turn(system="s", messages=history)
    history.append(UserMessage("second"))
    await provider.generate_turn(system="s", messages=history)

    assert [len(c["messages"]) for c in provider.calls] == [1, 2]

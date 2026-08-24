"""Anthropic adapter — wire-format translation, tested without a network.

The SDK client is replaced with a stub, so these tests need no API key and make
no request. What is under test is the translation between this repo's internal
tool vocabulary and Anthropic's Messages API shape.
"""

from __future__ import annotations

import types

import pytest

from app.llm.anthropic import (
    AnthropicProvider,
    _from_anthropic_response,
    _to_anthropic_messages,
    _to_anthropic_tools,
)
from app.llm.tools import (
    AssistantMessage,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)

SPEC = ToolSpec(
    name="get_latest_metric",
    description="Fetch a metric.",
    input_schema={"type": "object", "properties": {"metric": {"type": "string"}}},
)


def _text(text: str):
    return types.SimpleNamespace(type="text", text=text)


def _tool_use(id_: str, name: str, input_):
    return types.SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def _response(content, stop_reason="end_turn", usage=True):
    return types.SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=5)
        if usage
        else None,
    )


class _StubMessages:
    """Captures the request payload and returns a canned response."""

    def __init__(self, response):
        self.response = response
        self.captured: dict = {}

    async def create(self, **kwargs):
        self.captured = kwargs
        return self.response


def _provider(response, **kwargs) -> tuple[AnthropicProvider, _StubMessages]:
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="k", **kwargs)
    stub = _StubMessages(response)
    provider._client = types.SimpleNamespace(messages=stub)  # type: ignore[attr-defined]
    return provider, stub


# --------------------------------------------------------------------------- #
# Outbound translation
# --------------------------------------------------------------------------- #
def test_tools_translate_to_the_input_schema_key():
    assert _to_anthropic_tools([SPEC]) == [
        {
            "name": "get_latest_metric",
            "description": "Fetch a metric.",
            "input_schema": SPEC.input_schema,
        }
    ]


def test_user_message_translates():
    assert _to_anthropic_messages([UserMessage("hi")]) == [
        {"role": "user", "content": "hi"}
    ]


def test_assistant_tool_call_becomes_a_tool_use_block():
    msgs = _to_anthropic_messages(
        [
            AssistantMessage(
                content="looking",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="get_latest_metric",
                        arguments={"metric": "hba1c"},
                    ),
                ),
            )
        ]
    )
    assert msgs == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "looking"},
                {
                    "type": "tool_use",
                    "id": "c1",
                    "name": "get_latest_metric",
                    "input": {"metric": "hba1c"},
                },
            ],
        }
    ]


def test_assistant_message_with_no_text_emits_no_empty_text_block():
    """An empty text block is not valid content; the model often returns a
    tool call with no prose."""
    msgs = _to_anthropic_messages(
        [AssistantMessage(content="", tool_calls=(ToolCall("c1", "t", {}),))]
    )
    assert [b["type"] for b in msgs[0]["content"]] == ["tool_use"]


def test_all_tool_results_land_in_ONE_user_message():
    """Splitting parallel results across messages teaches the model to stop
    making parallel calls."""
    msgs = _to_anthropic_messages(
        [
            ToolResultMessage(
                results=(
                    ToolResult(call_id="c1", content="{}"),
                    ToolResult(call_id="c2", content="{}", is_error=True),
                )
            )
        ]
    )
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert [b["tool_use_id"] for b in msgs[0]["content"]] == ["c1", "c2"]
    assert msgs[0]["content"][1]["is_error"] is True


# --------------------------------------------------------------------------- #
# Inbound translation
# --------------------------------------------------------------------------- #
def test_response_with_tool_use_parses_to_a_turn():
    turn = _from_anthropic_response(
        _response(
            [_text("one moment"), _tool_use("c1", "get_latest_metric", {"metric": "hba1c"})],
            stop_reason="tool_use",
        )
    )
    assert turn.wants_tools
    assert turn.text == "one moment"
    assert turn.tool_calls[0].arguments == {"metric": "hba1c"}
    assert turn.usage == {"input_tokens": 10, "output_tokens": 5}


def test_plain_text_response_parses_to_an_end_turn():
    turn = _from_anthropic_response(_response([_text("hello")]))
    assert turn.text == "hello"
    assert not turn.wants_tools
    assert turn.stop_reason == "end_turn"


def test_multiple_text_blocks_are_joined():
    turn = _from_anthropic_response(_response([_text("part one. "), _text("part two.")]))
    assert turn.text == "part one. part two."


def test_string_encoded_tool_input_is_parsed():
    """Tool inputs may arrive with provider-specific JSON escaping — never
    string-match on the serialized form."""
    turn = _from_anthropic_response(
        _response([_tool_use("c1", "t", '{"metric": "hba1c"}')], stop_reason="tool_use")
    )
    assert turn.tool_calls[0].arguments == {"metric": "hba1c"}


def test_malformed_tool_input_does_not_raise():
    """A bad call must become a rejectable empty-argument call, never an
    exception that kills the turn."""
    turn = _from_anthropic_response(
        _response([_tool_use("c1", "t", "{not json")], stop_reason="tool_use")
    )
    assert turn.tool_calls[0].arguments == {}


def test_missing_usage_is_tolerated():
    turn = _from_anthropic_response(_response([_text("hi")], usage=False))
    assert turn.usage is None


def test_refusal_stop_reason_is_preserved():
    """A safety refusal is HTTP 200 with stop_reason 'refusal' — it must not
    look like a normal end_turn to the agent loop."""
    turn = _from_anthropic_response(_response([], stop_reason="refusal"))
    assert turn.stop_reason == "refusal"
    assert not turn.wants_tools


# --------------------------------------------------------------------------- #
# Request construction
# --------------------------------------------------------------------------- #
async def test_thinking_is_omitted_by_default():
    """Thinking is model-gated: adaptive needs a 4.6+ model, and Haiku 4.5
    rejects it with a 400. Never send it unless explicitly configured."""
    provider, stub = _provider(_response([_text("ok")]))
    await provider.generate_turn(system="s", messages=[UserMessage("hi")])
    assert "thinking" not in stub.captured
    assert stub.captured["model"] == "claude-haiku-4-5"
    assert stub.captured["system"] == "s"


async def test_thinking_adaptive_is_sent_when_configured():
    provider, stub = _provider(_response([_text("ok")]), thinking="adaptive")
    await provider.generate_turn(system="s", messages=[UserMessage("hi")])
    assert stub.captured["thinking"] == {"type": "adaptive"}


async def test_tools_are_omitted_when_none_are_offered():
    """Sending an empty tools list is not the same as sending none — the
    forced-final-answer call in the agent loop depends on this."""
    provider, stub = _provider(_response([_text("ok")]))
    await provider.generate_turn(system="s", messages=[UserMessage("hi")], tools=())
    assert "tools" not in stub.captured


async def test_tools_are_sent_when_offered():
    provider, stub = _provider(_response([_text("ok")]))
    await provider.generate_turn(
        system="s", messages=[UserMessage("hi")], tools=[SPEC]
    )
    assert [t["name"] for t in stub.captured["tools"]] == ["get_latest_metric"]


async def test_generate_delegates_to_generate_turn_and_returns_text():
    """The legacy text path must keep working through the same adapter."""
    provider, stub = _provider(_response([_text("a plain answer")]))
    out = await provider.generate(system="s", user="u")
    assert out == "a plain answer"
    assert stub.captured["messages"] == [{"role": "user", "content": "u"}]
    assert "tools" not in stub.captured


async def test_model_name_is_exposed_for_receipts():
    provider, _ = _provider(_response([_text("ok")]))
    assert provider.model_name == "claude-haiku-4-5"


def test_provider_rejects_a_date_suffixed_model_id():
    """Model IDs carry no date suffix. A stale id fails at construction rather
    than as a 404 on the first real request."""
    with pytest.raises(ValueError, match="date suffix"):
        AnthropicProvider(model="claude-haiku-4-5-20251001", api_key="k")

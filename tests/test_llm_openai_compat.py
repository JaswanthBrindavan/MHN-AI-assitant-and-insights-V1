"""OpenAI-compatible adapter — the self-hosting path.

Speaks the format used by vLLM, Ollama, llama.cpp, LM Studio and every hosted
OpenAI-compatible gateway, so the deployment can move between them without
touching anything above the adapter.

Open-weight models emit malformed tool arguments more often than hosted ones;
parsing is deliberately forgiving so a bad call becomes a rejectable empty call
rather than an exception that kills the turn.
"""

from __future__ import annotations

import json

from app.llm.openai_compat import (
    OpenAICompatibleProvider,
    _from_openai_response,
    _to_openai_messages,
    _to_openai_tools,
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


def _payload(message: dict, finish_reason: str = "stop", usage: dict | None = None):
    body: dict = {"choices": [{"finish_reason": finish_reason, "message": message}]}
    if usage is not None:
        body["usage"] = usage
    return body


# --------------------------------------------------------------------------- #
# Outbound translation
# --------------------------------------------------------------------------- #
def test_tools_use_the_function_envelope():
    assert _to_openai_tools([SPEC]) == [
        {
            "type": "function",
            "function": {
                "name": "get_latest_metric",
                "description": "Fetch a metric.",
                "parameters": SPEC.input_schema,
            },
        }
    ]


def test_tool_call_arguments_serialize_to_a_json_string():
    msgs = _to_openai_messages(
        [
            AssistantMessage(
                content="",
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
    assert json.loads(msgs[0]["tool_calls"][0]["function"]["arguments"]) == {
        "metric": "hba1c"
    }
    assert msgs[0]["tool_calls"][0]["type"] == "function"


def test_each_tool_result_becomes_its_own_tool_role_message():
    """The inverse of Anthropic's rule: OpenAI keys each result to its call in
    a separate message. Same internal type, opposite wire shape."""
    msgs = _to_openai_messages(
        [
            ToolResultMessage(
                results=(
                    ToolResult(call_id="c1", content="{}"),
                    ToolResult(call_id="c2", content="{}"),
                )
            )
        ]
    )
    assert [m["role"] for m in msgs] == ["tool", "tool"]
    assert [m["tool_call_id"] for m in msgs] == ["c1", "c2"]


def test_assistant_content_is_none_when_empty():
    """The API rejects an empty-string content on a tool-call message."""
    msgs = _to_openai_messages(
        [AssistantMessage(content="", tool_calls=(ToolCall("c1", "t", {}),))]
    )
    assert msgs[0]["content"] is None


# --------------------------------------------------------------------------- #
# Inbound translation
# --------------------------------------------------------------------------- #
def test_response_with_tool_calls_parses_to_a_turn():
    turn = _from_openai_response(
        _payload(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "get_latest_metric",
                            "arguments": json.dumps({"metric": "hba1c"}),
                        },
                    }
                ],
            },
            finish_reason="tool_calls",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
    )
    assert turn.wants_tools
    assert turn.tool_calls[0].arguments == {"metric": "hba1c"}
    assert turn.usage == {"input_tokens": 10, "output_tokens": 5}


def test_plain_text_response_parses():
    turn = _from_openai_response(_payload({"content": "hello"}))
    assert turn.text == "hello"
    assert turn.stop_reason == "end_turn"
    assert not turn.wants_tools


def test_malformed_tool_arguments_do_not_raise():
    """Open-weight models emit invalid JSON more often than hosted ones."""
    turn = _from_openai_response(
        _payload(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "t", "arguments": "{not json"},
                    }
                ],
            },
            finish_reason="tool_calls",
        )
    )
    assert turn.tool_calls[0].arguments == {}


def test_finish_reasons_map_to_the_internal_vocabulary():
    cases = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
        "content_filter": "refusal",
    }
    for wire, internal in cases.items():
        turn = _from_openai_response(_payload({"content": "x"}, finish_reason=wire))
        assert turn.stop_reason == internal, wire


def test_an_unknown_finish_reason_degrades_to_end_turn():
    """A gateway inventing its own value must not look like a tool request."""
    turn = _from_openai_response(_payload({"content": "x"}, finish_reason="weird"))
    assert turn.stop_reason == "end_turn"
    assert not turn.wants_tools


def test_a_tool_call_finish_reason_with_no_calls_does_not_loop():
    """Guards the agent loop: wants_tools needs BOTH halves."""
    turn = _from_openai_response(
        _payload({"content": "x", "tool_calls": []}, finish_reason="tool_calls")
    )
    assert not turn.wants_tools


def test_an_empty_response_does_not_raise():
    assert _from_openai_response({}).text == ""


def test_missing_usage_is_tolerated():
    assert _from_openai_response(_payload({"content": "x"})).usage is None


# --------------------------------------------------------------------------- #
# Request construction
# --------------------------------------------------------------------------- #
class _StubResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _StubClient:
    def __init__(self, body):
        self._body = body
        self.captured: dict = {}

    async def post(self, url, **kwargs):
        self.captured = {"url": url, **kwargs}
        return _StubResponse(self._body)


def _provider(body, **kwargs):
    provider = OpenAICompatibleProvider(
        base_url="http://local:8000/v1", model="qwen2.5", **kwargs
    )
    stub = _StubClient(body)
    provider._client_factory = lambda _timeout: stub  # type: ignore[attr-defined]
    return provider, stub


async def test_system_prompt_becomes_the_first_message():
    """Unlike Anthropic, the system prompt is a message, not a field."""
    provider, stub = _provider(_payload({"content": "ok"}))
    await provider.generate_turn(system="rules", messages=[UserMessage("hi")])
    assert stub.captured["json"]["messages"][0] == {
        "role": "system",
        "content": "rules",
    }


async def test_tools_are_omitted_when_none_are_offered():
    provider, stub = _provider(_payload({"content": "ok"}))
    await provider.generate_turn(system="s", messages=[UserMessage("hi")], tools=())
    assert "tools" not in stub.captured["json"]


async def test_api_key_becomes_a_bearer_header():
    provider, stub = _provider(_payload({"content": "ok"}), api_key="secret")
    await provider.generate_turn(system="s", messages=[UserMessage("hi")])
    assert stub.captured["headers"]["Authorization"] == "Bearer secret"


async def test_no_auth_header_when_self_hosted_without_a_key():
    provider, stub = _provider(_payload({"content": "ok"}))
    await provider.generate_turn(system="s", messages=[UserMessage("hi")])
    assert "Authorization" not in stub.captured["headers"]


async def test_generate_delegates_and_returns_text():
    provider, stub = _provider(_payload({"content": "a plain answer"}))
    assert await provider.generate(system="s", user="u") == "a plain answer"
    assert stub.captured["json"]["messages"][-1] == {"role": "user", "content": "u"}

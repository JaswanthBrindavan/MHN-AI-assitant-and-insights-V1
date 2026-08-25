"""Anthropic Messages API adapter (official SDK).

Translates this repo's internal tool vocabulary to and from Anthropic's wire
format. The SDK owns retries, timeouts, and typed errors — do not re-implement
them here.

Model IDs carry NO date suffix: "claude-haiku-4-5", never
"claude-haiku-4-5-20251001". A stale id is rejected at construction so it fails
on deploy rather than as a 404 on the first patient-facing request.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Sequence

from anthropic import AsyncAnthropic

from app.llm.tools import (
    AssistantMessage,
    LLMTurn,
    Message,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)

logger = logging.getLogger("davi.llm")

# Trailing -YYYYMMDD on a model id. Current ids are complete without one.
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")

# Thinking is model-gated: "adaptive" requires a 4.6+ model. Haiku 4.5 and
# every OpenAI-compatible endpoint reject it, so it is opt-in only.
_THINKING_ADAPTIVE = {"type": "adaptive"}


def _to_anthropic_tools(tools: Sequence[ToolSpec]) -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in tools
    ]


def _to_anthropic_messages(messages: Sequence[Message]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if isinstance(m, UserMessage):
            if m.attachments:
                # Images FIRST, then the question: the model reads better when
                # it has seen the image before being asked about it.
                out.append(
                    {
                        "role": "user",
                        "content": [
                            *m.attachments,
                            # An empty text block is not valid content — the
                            # AssistantMessage branch below guards the same
                            # thing for the same reason.
                            *(
                                [{"type": "text", "text": m.content}]
                                if m.content
                                else []
                            ),
                        ],
                    }
                )
            else:
                out.append({"role": "user", "content": m.content})
        elif isinstance(m, AssistantMessage):
            blocks: list[dict] = []
            # Only emit a text block when there is text: an empty one is not
            # valid content, and a tool call with no prose is the common case.
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for call in m.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            out.append({"role": "assistant", "content": blocks})
        elif isinstance(m, ToolResultMessage):
            # ONE user message carrying EVERY result from that assistant turn.
            # Splitting them teaches the model to stop calling tools in
            # parallel.
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": r.call_id,
                            "content": r.content,
                            "is_error": r.is_error,
                        }
                        for r in m.results
                    ],
                }
            )
    return out


# Anthropic renders the cacheable prompt in a fixed order: tools, then
# system, then messages. The breakpoint goes on the FIRST system block, which
# is the stable prefix; because tools come before system, that one mark covers
# the tool schemas too. This matters here: the tool schemas are the LARGER half
# of what is stable, and the system rules alone (~850 tokens) are UNDER the
# minimum. See project_docs/task-23-caching.md for the measurement.
#
# Do not "fix" this to mark the last block. The last block is the volatile
# tail, and marking it would rewrite the cache on every single turn.
_CACHE_CONTROL = {"type": "ephemeral"}

# Below this, Anthropic silently declines to cache and the request behaves
# exactly as if no breakpoint were set. Named so the reason is greppable when
# somebody wonders why the hit rate is zero.
MIN_CACHEABLE_TOKENS = 1024


def _to_system_blocks(system: str | Sequence[str]) -> str | list[dict]:
    """Render the system prompt, marking a cache breakpoint after the prefix.

    A plain string is passed through untouched — no breakpoint, no behaviour
    change for the callers that do not separate their prompt.

    A sequence means the caller has split stable from volatile: element 0 is
    the byte-identical prefix and gets the breakpoint. Everything after it
    varies per turn and must sit AFTER the mark, or it would invalidate the
    cache on every single call — the classic way to pay the 25% cache-write
    premium forever and never once read from it.
    """
    if isinstance(system, str):
        return system

    parts = [p for p in system if p]
    if not parts:
        return ""
    if len(parts) == 1:
        # Nothing volatile to separate; a breakpoint would still be valid but
        # buys nothing over a plain string.
        return parts[0]

    blocks: list[dict] = [
        {"type": "text", "text": parts[0], "cache_control": _CACHE_CONTROL}
    ]
    blocks.extend({"type": "text", "text": p} for p in parts[1:])
    return blocks


def _parse_arguments(raw) -> dict:
    """Tool inputs may arrive as a dict or as a JSON string, with
    provider-specific escaping. Never string-match on the serialized form, and
    never let a malformed call raise — the loop needs to see it and recover."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        logger.warning("malformed tool arguments from provider; rejecting call")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _from_anthropic_response(resp) -> LLMTurn:
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in resp.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            calls.append(
                ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=_parse_arguments(block.input),
                )
            )
    usage = None
    if getattr(resp, "usage", None) is not None:
        usage = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        }
        # Cache fields, when the provider reports them. Surfaced because a
        # cache breakpoint that silently fails to cache looks EXACTLY like one
        # that works — the reply is identical and only these numbers differ.
        for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            value = getattr(resp.usage, field, None)
            if value is not None:
                usage[field] = value
    return LLMTurn(
        text="".join(text_parts).strip(),
        tool_calls=tuple(calls),
        stop_reason=resp.stop_reason or "end_turn",
        usage=usage,
    )


class AnthropicProvider:
    """Tool-calling provider over the Anthropic Messages API."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        max_tokens: int = 4096,
        thinking: str = "off",
    ) -> None:
        if _DATE_SUFFIX_RE.search(model):
            raise ValueError(
                f"model id {model!r} carries a date suffix; current Anthropic "
                "model ids are complete without one (e.g. 'claude-haiku-4-5')"
            )
        self.model = model
        self.model_name = model
        self._max_tokens = max_tokens
        self._thinking = thinking
        client_kwargs: dict = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**client_kwargs)

    async def generate(self, *, system: str | Sequence[str], user: str) -> str:
        turn = await self.generate_turn(
            system=system, messages=[UserMessage(user)], tools=()
        )
        return turn.text

    async def generate_turn(
        self,
        *,
        system: str | Sequence[str],
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> LLMTurn:
        payload: dict = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "system": _to_system_blocks(system),
            "messages": _to_anthropic_messages(messages),
        }
        if self._thinking == "adaptive":
            payload["thinking"] = _THINKING_ADAPTIVE
        # Omit `tools` entirely rather than sending an empty list: the agent
        # loop's forced-final-answer call relies on offering no tools at all.
        if tools:
            payload["tools"] = _to_anthropic_tools(tools)
        resp = await self._client.messages.create(**payload)
        return _from_anthropic_response(resp)

    async def generate_stream(
        self,
        *,
        system: str | Sequence[str],
        messages: Sequence[Message],
    ) -> AsyncIterator[str]:
        """Yield text deltas. Tools are NOT offered here.

        Streaming happens after the tool rounds are done — an answer being
        composed from tool results is the only thing worth streaming, and
        interleaving tool calls into a token stream buys nothing.
        """
        payload: dict = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "system": _to_system_blocks(system),
            "messages": _to_anthropic_messages(messages),
        }
        if self._thinking == "adaptive":
            payload["thinking"] = _THINKING_ADAPTIVE
        async with self._client.messages.stream(**payload) as stream:
            async for text in stream.text_stream:
                yield text

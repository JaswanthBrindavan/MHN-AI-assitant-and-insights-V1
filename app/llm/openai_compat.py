"""OpenAI /chat/completions adapter over httpx — the self-hosting path.

Speaks the format used by vLLM, Ollama, llama.cpp, LM Studio, and every hosted
OpenAI-compatible gateway, so the deployment can move between them without
touching anything above the adapter. No vendor SDK, so a self-hosted setup
needs no extra dependency.

Parsing is deliberately forgiving: open-weight models emit malformed tool
arguments and invented finish reasons far more often than hosted ones, and a
bad response must become something the agent loop can see and recover from —
never an exception that kills a patient-facing turn.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence

import httpx

from app.llm.tools import (
    AssistantMessage,
    LLMTurn,
    Message,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
    join_system,
)

logger = logging.getLogger("davi.llm")

# Wire finish_reason -> internal stop_reason. Anything unrecognised degrades to
# end_turn: a gateway inventing its own value must never look like a request to
# call tools.
_FINISH_MAP = {
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}

_shared_client: httpx.AsyncClient | None = None


def _default_client(timeout: float) -> httpx.AsyncClient:
    """One pooled client per process — keep-alive skips the TCP+TLS handshake
    that a per-call client would pay on every message."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
    return _shared_client


def _to_openai_tools(tools: Sequence[ToolSpec]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]



def _to_openai_image(attachment: dict) -> dict:
    """Anthropic-shaped image block -> OpenAI image_url part.

    A data: URI rather than a link: the presigned URL is short-lived and
    pointing a third party at it would hand out access this service was
    careful to keep scoped.
    """
    source = attachment.get("source") or {}
    media = source.get("media_type", "image/jpeg")
    data = source.get("data", "")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media};base64,{data}"},
    }


def _to_openai_messages(messages: Sequence[Message]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if isinstance(m, UserMessage):
            if m.attachments:
                out.append(
                    {
                        "role": "user",
                        "content": [
                            *(_to_openai_image(a) for a in m.attachments),
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
            # content must be None, not "", on a tool-call message.
            msg: dict = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(c.arguments),
                        },
                    }
                    for c in m.tool_calls
                ]
            out.append(msg)
        elif isinstance(m, ToolResultMessage):
            # The inverse of Anthropic's rule: one message per result, keyed by
            # tool_call_id.
            out.extend(
                {"role": "tool", "tool_call_id": r.call_id, "content": r.content}
                for r in m.results
            )
    return out


def _parse_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        logger.warning("malformed tool arguments from provider; rejecting call")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _from_openai_response(data: dict) -> LLMTurn:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}

    calls: list[ToolCall] = []
    for raw in message.get("tool_calls") or []:
        fn = raw.get("function") or {}
        calls.append(
            ToolCall(
                id=raw.get("id", ""),
                name=fn.get("name", ""),
                arguments=_parse_arguments(fn.get("arguments")),
            )
        )

    usage_raw = data.get("usage") or {}
    usage = (
        {
            "input_tokens": usage_raw.get("prompt_tokens", 0),
            "output_tokens": usage_raw.get("completion_tokens", 0),
        }
        if usage_raw
        else None
    )

    return LLMTurn(
        text=(message.get("content") or "").strip(),
        tool_calls=tuple(calls),
        stop_reason=_FINISH_MAP.get(choice.get("finish_reason", "stop"), "end_turn"),
        usage=usage,
    )


class OpenAICompatibleProvider:
    """Tool-calling provider over any OpenAI-compatible /v1 endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 60.0,
        max_tokens: int = 800,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_name = model
        self._api_key = api_key
        self._timeout = timeout
        self._max_tokens = max_tokens
        # Indirection so tests can inject a stub without patching globals.
        self._client_factory = _default_client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

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
            # Unlike Anthropic, the system prompt is a message, not a field.
            "messages": [
                {"role": "system", "content": join_system(system)},
                *_to_openai_messages(messages),
            ],
            "temperature": 0,
            "stream": False,
            # This adapter sent NO output cap at all, so the ceiling was
            # whatever the server chose. An unbounded budget is what made a
            # reply cost ~25 s of generation.
            "max_tokens": self._max_tokens,
        }
        if tools:
            payload["tools"] = _to_openai_tools(tools)

        resp = await self._client_factory(self._timeout).post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return _from_openai_response(resp.json())

    async def generate_stream(
        self,
        *,
        system: str | Sequence[str],
        messages: Sequence[Message],
    ) -> AsyncIterator[str]:
        """Yield text deltas from an SSE /chat/completions stream."""
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": join_system(system)},
                *_to_openai_messages(messages),
            ],
            "temperature": 0,
            "stream": True,
        }
        client = self._client_factory(self._timeout)
        async with client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=self._headers(),
            timeout=self._timeout,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except ValueError:
                    # A malformed frame is not worth killing a reply over.
                    logger.warning("unparseable SSE frame; skipping")
                    continue
                choices = chunk.get("choices") or [{}]
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    yield delta

"""Model- and cloud-agnostic LLM providers.

Everything speaks plain HTTP via httpx — no vendor SDKs, no cloud lock-in:

* ``OpenAICompatibleProvider`` — any endpoint implementing the OpenAI
  ``/chat/completions`` contract: OpenAI, Azure OpenAI (compatible mode),
  Groq, Together, Fireworks, vLLM, LM Studio, Ollama, llama.cpp server…
* ``AnthropicProvider`` — the Anthropic Messages API.
* ``FakeProvider`` (app.llm.fake) — deterministic, for tests/offline.

Selected by ``LLM_PROVIDER`` env: fake | openai_compatible | anthropic |
ollama (alias of openai_compatible with the legacy OLLAMA_* settings).
"""

from __future__ import annotations

import httpx

# One pooled client for the process: keep-alive connections skip the TCP+TLS
# handshake (~150-400 ms against a cloud API) that a per-call client pays on
# every message. Lives for the process lifetime; providers are constructed
# per-request, so the pool cannot live on the instances.
_shared_client: httpx.AsyncClient | None = None


def _client(timeout: float) -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
    return _shared_client


class _UsageMixin:
    """Optional token accounting. Off unless ``record_usage()`` is called.

    Accumulates across every ``generate`` on this instance until reset, so a
    turn that calls the model twice (e.g. one grounding-corrective retry) sums
    correctly. Costs nothing on the hot path — ``handle_chat`` never touches it.
    """

    last_usage: dict[str, int] | None = None

    def record_usage(self) -> None:
        self.last_usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def _add_usage(self, input_tokens: int, output_tokens: int) -> None:
        if self.last_usage is not None:
            self.last_usage["input_tokens"] += input_tokens
            self.last_usage["output_tokens"] += output_tokens
            self.last_usage["calls"] += 1


class OpenAICompatibleProvider(_UsageMixin):
    """Chat completions against any OpenAI-compatible /v1 endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_name = model
        self._api_key = api_key
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def generate(self, *, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "stream": False,
        }
        resp = await _client(self._timeout).post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        self._add_usage(
            usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        )
        return data["choices"][0]["message"]["content"]


class AnthropicProvider(_UsageMixin):
    """Messages API against Anthropic (or any compatible gateway URL)."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        max_tokens: int = 1024,
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_name = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._timeout = timeout

    async def generate(self, *, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        resp = await _client(self._timeout).post(
            f"{self.base_url}/v1/messages",
            json=payload,
            headers=headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        self._add_usage(
            usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        )
        return "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )

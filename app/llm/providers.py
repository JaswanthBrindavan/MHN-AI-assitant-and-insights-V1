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


class OpenAICompatibleProvider:
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
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"]


class AnthropicProvider:
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
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/messages", json=payload, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
        return "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )

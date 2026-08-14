"""Ollama provider (OpenAI-compatible /v1 chat completions).

Not exercised by the test suite (no live LLM/network in tests). See the manual
smoke doc in the README for local Ollama usage.
"""

from __future__ import annotations

import httpx


class OllamaProvider:
    def __init__(self, base_url: str, model: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_name = model
        self._timeout = timeout

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
            resp = await client.post(f"{self.base_url}/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"]

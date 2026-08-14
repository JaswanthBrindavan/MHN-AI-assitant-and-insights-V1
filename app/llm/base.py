"""LLM provider protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """A minimal chat-completion provider.

    Implementations must be side-effect free apart from the network call and
    must never raise for ordinary completions (callers treat exceptions as a
    guardrail failure and degrade to a safe reply).
    """

    model_name: str

    async def generate(self, *, system: str, user: str) -> str: ...

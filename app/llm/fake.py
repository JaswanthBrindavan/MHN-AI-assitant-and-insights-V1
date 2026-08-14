"""Deterministic fake provider for tests and offline dev.

Returns scripted responses in order (for multi-step grounding tests), then a
safe generic default. Records calls for assertions.
"""

from __future__ import annotations


class FakeProvider:
    model_name = "fake"

    # A default that carries no clinical numbers (so grounding passes trivially)
    # and no diagnostic phrasing (so validation passes).
    DEFAULT = (
        "Thanks for your question. In general, steady habits — a balanced diet, "
        "regular activity, and routine check-ups — support wellbeing. For "
        "anything specific to you, it's best to talk with your doctor."
    )

    def __init__(self, responses: list[str] | None = None, default: str | None = None):
        self._responses = list(responses or [])
        self._default = default if default is not None else self.DEFAULT
        self.calls: list[tuple[str, str]] = []

    async def generate(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self._responses:
            return self._responses.pop(0)
        return self._default

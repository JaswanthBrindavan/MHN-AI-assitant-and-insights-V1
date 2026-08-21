"""Deterministic in-memory translator for tests and evals (no network)."""

from __future__ import annotations

from app.translate.service import SidecarTranslator


class FakeTranslator(SidecarTranslator):
    """Scriptable stand-in: canned detect result + prefix-marked translations.

    ``to_english``/``from_english`` map input text → output text; unmapped
    inputs get a visible prefix so tests can assert the call happened. Set
    ``fail=True`` to simulate a sidecar outage (every call returns None).
    """

    def __init__(
        self,
        detect_result: dict | None = None,
        to_english: dict[str, str] | None = None,
        from_english: dict[str, str] | None = None,
        fail: bool = False,
    ) -> None:
        super().__init__(base_url="http://fake-translator")
        self.detect_result = detect_result
        self.to_english = to_english or {}
        self.from_english = from_english or {}
        self.fail = fail
        self.calls: list[tuple[str, str]] = []  # (endpoint, text)

    async def detect(self, text: str) -> dict | None:
        self.calls.append(("detect", text))
        if self.fail:
            return None
        return self.detect_result

    async def translate(
        self, text: str, language: str, direction: str, script: str
    ) -> str | None:
        self.calls.append((direction, text))
        if self.fail:
            return None
        if direction == "to_english":
            return self.to_english.get(text, f"[en] {text}")
        return self.from_english.get(text, f"[{language}/{script}] {text}")

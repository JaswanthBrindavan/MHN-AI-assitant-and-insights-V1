"""English-pivot translation against the self-hosted translator sidecar.

The sidecar (``translator/`` in this repo, deployed as its own service) hosts
AI4Bharat models: IndicTrans2 (MIT) for En↔Indic translation, IndicXlit for
roman↔native transliteration, and IndicLID for romanized language ID. Davi
holds no model weights and sends text only to OUR sidecar — PHI never leaves
the deployment.

Flow per chat turn:

  detect (script ranges locally; sidecar /detect for Latin-script text)
  → translate the message to English (sidecar /translate)
  → the ENTIRE deterministic pipeline — triage floor, scope, intent routing,
    abilities, drug lookup, RAG — runs on English, unchanged
  → translate the reply back, mirroring the user's script (native/romanized)

Everything here is fail-open: unset base URL, network errors, low-confidence
detection, or a digit-fidelity failure all degrade to English (plus the LLM
reply-language directive), never an exception. There are no per-language
reply templates anywhere — every reply, the deterministic safety directives
included, is composed in English and translated by the sidecar, guarded by
the digit-fidelity check below (IndicTrans2 is a pure MT model: it never
refuses, unlike safety-tuned LLM translators).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass

import httpx

from app.config import get_settings
from app.i18n.language import detect_language

logger = logging.getLogger("davi.translate")

# Languages the sidecar serves (the IndicTrans2 coverage we expose).
SUPPORTED_LANGUAGES = frozenset(
    {"hi", "bn", "pa", "gu", "or", "ta", "te", "kn", "ml", "mr"}
)

# Below this /detect confidence a Latin-script message is treated as English.
MIN_DETECT_CONFIDENCE = 0.5


@dataclass(frozen=True)
class InboundPivot:
    """Outcome of the inbound half of the pivot for one message."""

    language: str  # base code: "te", "hi", ... or "en"
    script: str  # "native" | "latin"
    english_text: str  # what the pipeline should run on
    active: bool  # True only when the message was actually translated

    @property
    def display_language(self) -> str:
        """BCP-47ish code reported to clients ("te", "te-Latn", "en")."""
        if self.language == "en":
            return "en"
        return f"{self.language}-Latn" if self.script == "latin" else self.language


class SidecarTranslator:
    """Thin httpx client for the translator sidecar. Every failure → None."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 8.0,
        token: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _post(self, path: str, payload: dict) -> dict | None:
        try:
            if self._client is not None:
                resp = await self._client.post(
                    f"{self.base_url}{path}", json=payload,
                    headers=self._headers(), timeout=self.timeout,
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        f"{self.base_url}{path}", json=payload,
                        headers=self._headers(),
                    )
            if resp.status_code != 200:
                logger.warning(
                    "translator sidecar %s -> HTTP %s", path, resp.status_code
                )
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 — fail open, never crash a chat turn
            logger.warning("translator sidecar %s failed", path, exc_info=True)
            return None

    async def detect(self, text: str) -> dict | None:
        """{"language": "te"|"en"|..., "script": "native"|"latin",
        "confidence": float} or None."""
        return await self._post("/detect", {"text": text})

    async def translate(
        self, text: str, language: str, direction: str, script: str
    ) -> str | None:
        """direction: "to_english" | "from_english". Returns text or None."""
        data = await self._post(
            "/translate",
            {"text": text, "language": language,
             "direction": direction, "script": script},
        )
        if data is None:
            return None
        out = data.get("text")
        return out if isinstance(out, str) and out.strip() else None


def get_translator() -> SidecarTranslator | None:
    """The configured sidecar client, or None when the feature is off."""
    settings = get_settings()
    if not settings.translate_base_url:
        return None
    return SidecarTranslator(
        settings.translate_base_url,
        timeout=settings.translate_timeout_seconds,
        token=settings.translate_token,
    )


def _split_local(code: str) -> tuple[str, str]:
    """Local detect_language() code → (base language, script)."""
    return ("en", "latin") if code == "en" else (code, "native")


async def pivot_inbound(
    message: str, translator: SidecarTranslator | None
) -> InboundPivot:
    """Detect the message's language and translate it to English.

    Native-script detection is deterministic (Unicode ranges) and authoritative;
    the sidecar only refines the Devanagari hi/mr ambiguity. Latin-script
    language ID is entirely the sidecar's job (IndicLID) — without a sidecar,
    Latin-script text is treated as English.
    """
    local = detect_language(message)
    base, script = _split_local(local)

    if translator is None or len(message.strip()) < 3:
        return InboundPivot(base, script, message, active=False)

    if script == "native":
        lang = base
        if base == "hi":
            # Devanagari is shared by Hindi and Marathi — one /detect call
            # picks the right IndicTrans2 source tag.
            det = await translator.detect(message)
            if det is not None and det.get("language") in {"hi", "mr"}:
                lang = str(det["language"])
        english = await translator.translate(message, lang, "to_english", "native")
        if english is not None:
            return InboundPivot(lang, "native", english, active=True)
        return InboundPivot(lang, "native", message, active=False)

    # Latin script: the sidecar decides between English and romanized Indic.
    det = await translator.detect(message)
    if det is None:
        return InboundPivot(base, script, message, active=False)
    lang = str(det.get("language", "en"))
    confidence = float(det.get("confidence", 0.0) or 0.0)
    if lang in SUPPORTED_LANGUAGES and confidence >= MIN_DETECT_CONFIDENCE:
        english = await translator.translate(message, lang, "to_english", "latin")
        if english is not None:
            return InboundPivot(lang, "latin", english, active=True)
        return InboundPivot(lang, "latin", message, active=False)
    return InboundPivot("en", "latin", message, active=False)


_DIGITS_RE = re.compile(r"\d+")


def digits_preserved(source: str, translated: str) -> bool:
    """True when every digit sequence survived translation unchanged.

    Dosages, lab values, and helpline numbers must never be corrupted by the
    MT model — a mismatch fails the whole translation (English fallback).
    """
    return Counter(_DIGITS_RE.findall(source)) == Counter(
        _DIGITS_RE.findall(translated)
    )


async def pivot_outbound(
    english_text: str,
    pivot: InboundPivot,
    translator: SidecarTranslator | None,
) -> str | None:
    """Translate the English reply back into the user's language and script.

    None means "keep the English reply" — sidecar failure, empty output, or a
    numeric-fidelity failure all fall back rather than risking a bad medical
    translation.
    """
    if translator is None or not pivot.active or not english_text.strip():
        return None
    out = await translator.translate(
        english_text, pivot.language, "from_english", pivot.script
    )
    if out is None:
        return None
    if len(out.strip()) < max(1, len(english_text) // 8):
        logger.warning("translated reply suspiciously short; keeping English")
        return None
    if not digits_preserved(english_text, out):
        logger.warning("digit mismatch after translation; keeping English")
        return None
    return out

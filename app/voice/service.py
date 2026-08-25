"""Voice — speech in, speech out, through a self-hosted sidecar.

Same pattern as ``translator/``: the models are ours, so PHI never leaves the
deployment. Empty base URL means the feature is simply off.

**The ordering rule that matters.** Transcription happens BEFORE anything else
in the pipeline — before the triage floor, before scope, before routing. A
spoken red flag has to reach the floor as text, or the whole safety design is
bypassed by the input method. There is no separate "voice pipeline"; there is
one pipeline, and voice is a way of getting text into it.

**Low confidence asks rather than guesses.** A misheard symptom is a safety
issue, not a UX annoyance: "I can breathe" and "I can't breathe" differ by one
phoneme and by everything else. Below the confidence floor the caller is told to
confirm rather than handed a transcript to act on.

Fail-open on synthesis (the reader still gets the text), fail-CLOSED on
transcription (a turn is not attempted on words nobody is sure of).
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config import get_settings
from app.i18n.language import LANGUAGE_NAMES

logger = logging.getLogger("davi.voice")


class HttpPoster(Protocol):
    """The only thing this module needs from an HTTP client.

    Narrower than httpx.AsyncClient deliberately — it states the real
    requirement and lets a test supply a stub without impersonating a full
    client.
    """

    async def post(self, url: str, **kwargs: Any) -> Any: ...


# Below this, the transcript is offered back for confirmation instead of being
# acted on. Deliberately conservative — the cost of asking is a second of the
# reader's time; the cost of mishearing a symptom is much higher.
MIN_TRANSCRIPT_CONFIDENCE = 0.65

# A note longer than this is almost certainly not a health question, and
# transcribing it would be a slow way to find that out.
MAX_AUDIO_BYTES = 10 * 1024 * 1024

ALLOWED_AUDIO_TYPES = frozenset(
    {"audio/ogg", "audio/opus", "audio/mpeg", "audio/mp4", "audio/wav",
     "audio/webm", "audio/x-m4a", "audio/aac"}
)


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    confidence: float

    @property
    def confident(self) -> bool:
        return self.confidence >= MIN_TRANSCRIPT_CONFIDENCE and bool(
            self.text.strip()
        )

    def confirmation_prompt(self) -> str:
        """What to say when the transcript is not trustworthy enough to act on.

        It quotes what was heard rather than asking the reader to repeat
        themselves blind — if it is nearly right, confirming is quicker than
        recording again.
        """
        language = LANGUAGE_NAMES.get(self.language, "")
        note = f" (I heard it as {language})" if language else ""
        if not self.text.strip():
            return (
                "Sorry — I couldn't make that out at all. Could you try again, "
                "or type it instead?"
            )
        return (
            f"I want to make sure I heard you correctly{note}. Did you say: "
            f'"{self.text.strip()}"? If not, please tell me again or type it — '
            "I would rather check than answer the wrong question."
        )


class VoiceSidecar:
    """Thin client for the self-hosted voice sidecar. Every failure -> None."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 30.0,
        client: HttpPoster | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
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
                async with httpx.AsyncClient(timeout=self.timeout) as owned:
                    resp = await owned.post(
                        f"{self.base_url}{path}", json=payload,
                        headers=self._headers(),
                    )
            if resp.status_code != 200:
                logger.warning("voice sidecar %s -> HTTP %s", path, resp.status_code)
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 — never crash a turn
            logger.warning("voice sidecar %s failed", path, exc_info=True)
            return None

    async def transcribe(
        self, audio: bytes, content_type: str, language_hint: str = ""
    ) -> Transcript | None:
        data = await self._post(
            "/transcribe",
            {
                "audio": base64.standard_b64encode(audio).decode("ascii"),
                "content_type": content_type,
                "language_hint": language_hint,
            },
        )
        if data is None:
            return None
        text = data.get("text")
        if not isinstance(text, str):
            return None
        try:
            confidence = float(data.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return Transcript(
            text=text.strip(),
            language=str(data.get("language", "en")),
            confidence=confidence,
        )

    async def synthesize(self, text: str, language: str = "en") -> bytes | None:
        data = await self._post(
            "/speak", {"text": text, "language": language}
        )
        if data is None:
            return None
        encoded = data.get("audio")
        if not isinstance(encoded, str) or not encoded:
            return None
        try:
            return base64.standard_b64decode(encoded)
        except Exception:  # noqa: BLE001
            logger.warning("voice sidecar returned undecodable audio")
            return None


def get_sidecar() -> VoiceSidecar | None:
    """The configured sidecar, or None when voice is off."""
    settings = get_settings()
    if not settings.voice_base_url:
        return None
    return VoiceSidecar(
        settings.voice_base_url,
        token=settings.voice_token,
        timeout=settings.voice_timeout_seconds,
    )


def audio_acceptable(content_type: str, size: int) -> tuple[bool, str]:
    """Cheap checks before anything is sent anywhere."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in ALLOWED_AUDIO_TYPES:
        return False, f"unsupported audio type: {ctype or 'unknown'}"
    if size <= 0:
        return False, "empty audio"
    if size > MAX_AUDIO_BYTES:
        return False, "audio too long"
    return True, ""

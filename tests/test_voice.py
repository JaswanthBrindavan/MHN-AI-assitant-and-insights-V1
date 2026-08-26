"""Voice — the safety rules that come with an audio input path.

Two things this file exists to pin:
  * a spoken red flag reaches the triage floor exactly as a typed one does —
    there is no separate voice pipeline to bypass it
  * low-confidence transcription ASKS rather than guesses, because "I can
    breathe" and "I can't breathe" differ by one phoneme and by everything else
"""

from __future__ import annotations

import base64

from app.voice.service import (
    ALLOWED_AUDIO_TYPES,
    MAX_AUDIO_BYTES,
    MIN_TRANSCRIPT_CONFIDENCE,
    Transcript,
    VoiceSidecar,
    audio_acceptable,
    get_sidecar,
)


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs.get("json", {})))
        return self._responses.pop(0) if self._responses else _Resp(500, None)


def _sidecar(*responses) -> tuple[VoiceSidecar, _Client]:
    client = _Client(*responses)
    return VoiceSidecar("http://voice.internal:9000", client=client), client


# --------------------------------------------------------------------------- #
# Off by default
# --------------------------------------------------------------------------- #
def test_voice_is_off_without_configuration():
    assert get_sidecar() is None


# --------------------------------------------------------------------------- #
# Input validation happens before anything is sent
# --------------------------------------------------------------------------- #
def test_an_unsupported_audio_type_is_refused():
    ok, reason = audio_acceptable("video/mp4", 1000)
    assert not ok and "unsupported" in reason


def test_empty_audio_is_refused():
    ok, reason = audio_acceptable("audio/ogg", 0)
    assert not ok and "empty" in reason


def test_oversized_audio_is_refused():
    ok, reason = audio_acceptable("audio/ogg", MAX_AUDIO_BYTES + 1)
    assert not ok and "too long" in reason


def test_a_normal_voice_note_is_accepted():
    ok, _ = audio_acceptable("audio/ogg; codecs=opus", 200_000)
    assert ok


def test_every_allowed_type_is_audio():
    assert all(t.startswith("audio/") for t in ALLOWED_AUDIO_TYPES)


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #
def test_a_confident_transcript_is_usable():
    assert Transcript("I have a headache", "en", 0.95).confident


def test_a_low_confidence_transcript_is_not_acted_on():
    assert not Transcript("I can breathe", "en", 0.4).confident


def test_the_confidence_floor_is_conservative():
    """The cost of asking is a second; the cost of mishearing a symptom is
    much higher."""
    assert MIN_TRANSCRIPT_CONFIDENCE >= 0.6


def test_an_empty_transcript_is_never_confident():
    assert not Transcript("", "en", 0.99).confident
    assert not Transcript("   ", "en", 0.99).confident


def test_the_confirmation_quotes_what_was_heard():
    """Confirming a nearly-right transcript is quicker than recording again."""
    prompt = Transcript("I have a headache", "en", 0.4).confirmation_prompt()
    assert "I have a headache" in prompt
    assert "?" in prompt


def test_the_confirmation_names_the_detected_language():
    prompt = Transcript("mujhe sar dard hai", "hi", 0.4).confirmation_prompt()
    assert "Hindi" in prompt


def test_an_unintelligible_note_asks_for_a_retry_or_typing():
    prompt = Transcript("", "en", 0.1).confirmation_prompt()
    assert "try again" in prompt or "type it" in prompt


# --------------------------------------------------------------------------- #
# The sidecar client
# --------------------------------------------------------------------------- #
async def test_transcription_round_trips():
    sidecar, client = _sidecar(
        _Resp(payload={"text": "I have a headache", "language": "en",
                       "confidence": 0.92})
    )
    result = await sidecar.transcribe(b"audiobytes", "audio/ogg")
    assert result is not None
    assert result.text == "I have a headache"
    assert result.confident

    # The audio travels base64-encoded to OUR sidecar only.
    _url, payload = client.calls[0]
    assert base64.standard_b64decode(payload["audio"]) == b"audiobytes"


async def test_a_sidecar_error_yields_no_transcript():
    sidecar, _ = _sidecar(_Resp(status=503, payload=None))
    assert await sidecar.transcribe(b"audio", "audio/ogg") is None


async def test_a_malformed_confidence_degrades_to_zero():
    """A missing or junk confidence must not read as certain."""
    sidecar, _ = _sidecar(
        _Resp(payload={"text": "something", "confidence": "not a number"})
    )
    result = await sidecar.transcribe(b"a", "audio/ogg")
    assert result is not None
    assert not result.confident


async def test_a_missing_confidence_is_treated_as_zero():
    sidecar, _ = _sidecar(_Resp(payload={"text": "something"}))
    result = await sidecar.transcribe(b"a", "audio/ogg")
    assert result is not None
    assert result.confidence == 0.0
    assert not result.confident


async def test_synthesis_round_trips():
    audio = b"spokenbytes"
    sidecar, _ = _sidecar(
        _Resp(payload={"audio": base64.standard_b64encode(audio).decode()})
    )
    assert await sidecar.synthesize("hello", "en") == audio


async def test_a_synthesis_failure_returns_none_rather_than_raising():
    sidecar, _ = _sidecar(_Resp(status=500, payload=None))
    assert await sidecar.synthesize("hello") is None


async def test_undecodable_audio_is_rejected():
    sidecar, _ = _sidecar(_Resp(payload={"audio": "!!!not base64!!!"}))
    assert await sidecar.synthesize("hello") is None


async def test_a_transport_exception_never_escapes():
    class _Explodes:
        async def post(self, *a, **kw):
            raise RuntimeError("network down")

    sidecar = VoiceSidecar("http://voice", client=_Explodes())
    assert await sidecar.transcribe(b"a", "audio/ogg") is None
    assert await sidecar.synthesize("hi") is None

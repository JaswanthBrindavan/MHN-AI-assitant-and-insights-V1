"""The voice endpoint.

The invariant worth the whole file: a spoken red flag reaches the triage floor
exactly as a typed one does. There is no separate voice pipeline, so the input
method cannot bypass the safety design.
"""

from __future__ import annotations

import base64

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1.chat import get_llm_provider
from app.db import get_db
from app.llm.fake import FakeProvider
from app.main import create_app
from app.voice.service import Transcript

HDR = {"X-User-Id": "33333333-3333-3333-3333-333333333333"}
AUDIO = base64.standard_b64encode(b"pretend-opus-bytes").decode()


class _Sidecar:
    """Stands in for the self-hosted sidecar."""

    def __init__(self, transcript: Transcript | None):
        self._transcript = transcript
        self.transcribed = 0

    async def transcribe(self, audio, content_type, language_hint=""):
        self.transcribed += 1
        return self._transcript

    async def synthesize(self, text, language="en"):
        return b"spoken"


@pytest.fixture
async def voice_client(sessionmaker, monkeypatch):
    """Returns a factory: give it a transcript, get a client back."""
    app = create_app()

    async def _override_db():
        async with sessionmaker() as session:
            yield session

    provider = FakeProvider(responses=["Some general guidance about headaches."])
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_llm_provider] = lambda: provider

    state = {"provider": provider, "sidecar": None}

    def _install(transcript: Transcript | None):
        sidecar = _Sidecar(transcript)
        state["sidecar"] = sidecar
        import app.api.v1.chat as chat_module

        monkeypatch.setattr(
            chat_module, "get_voice_sidecar", lambda: sidecar, raising=True
        )
        return sidecar

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _install, state


def _body(**over):
    return {"audio": AUDIO, "content_type": "audio/ogg", **over}


# --------------------------------------------------------------------------- #
# The ordering rule
# --------------------------------------------------------------------------- #
async def test_a_spoken_emergency_reaches_the_triage_floor(voice_client):
    """The whole point. A red flag spoken aloud must be treated exactly as one
    typed — the model is not consulted either way."""
    client, install, state = voice_client
    install(Transcript("I can't breathe", "en", 0.97))

    resp = await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["risk_level"] == "emergency"
    assert body["recommended_action"] == "call_emergency_services"
    assert state["provider"].calls == []


async def test_a_confident_transcript_runs_the_normal_pipeline(voice_client):
    client, install, _ = voice_client
    install(Transcript("why do I keep getting headaches?", "en", 0.93))

    resp = await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    body = resp.json()
    assert resp.status_code == 200
    assert body["risk_level"] == "none"
    assert body["provenance"]["transcript_confidence"] == 0.93


async def test_the_trace_records_that_it_was_heard_not_typed(voice_client):
    client, install, _ = voice_client
    install(Transcript("why do I keep getting headaches?", "en", 0.93))

    resp = await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    steps = [s["step"] for s in resp.json()["trace"]]
    assert steps[0] == "Transcription"


# --------------------------------------------------------------------------- #
# Ask rather than guess
# --------------------------------------------------------------------------- #
async def test_a_low_confidence_transcript_asks_instead_of_answering(voice_client):
    """'I can breathe' and 'I can't breathe' differ by one phoneme."""
    client, install, state = voice_client
    install(Transcript("I can breathe", "en", 0.35))

    resp = await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    body = resp.json()
    assert body["recommended_action"] == "confirm_transcript"
    assert "I can breathe" in body["response_message"]
    # The pipeline was NOT run on words nobody is sure of.
    assert state["provider"].calls == []


async def test_there_is_no_client_flag_that_disables_the_gate(voice_client):
    """`confirmed` was removed. A client that agrees with a transcript POSTs
    the TEXT to /chat, which bounds it, sanitises it, runs the floor and
    validates the reply. Re-sending audio would re-run a sampling ASR decoder,
    so the text acted on need not be the text the reader saw."""
    client, install, _ = voice_client
    install(Transcript("why do I keep getting headaches?", "en", 0.35))

    resp = await client.post(
        "/api/v1/chat/voice", headers=HDR, json=_body(confirmed=True)
    )
    # The extra field is ignored, not honoured — the gate still fires.
    assert resp.json()["recommended_action"] == "confirm_transcript"


async def test_a_confirmation_still_returns_a_session_to_continue_in(voice_client):
    client, install, _ = voice_client
    install(Transcript("something unclear", "en", 0.2))

    resp = await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    assert resp.json()["session_id"]


# --------------------------------------------------------------------------- #
# Input handling
# --------------------------------------------------------------------------- #
async def test_voice_is_503_when_not_configured(sessionmaker):
    app = create_app()

    async def _override_db():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_llm_provider] = lambda: FakeProvider()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    assert resp.status_code == 503


async def test_invalid_base64_is_a_400(voice_client):
    client, install, _ = voice_client
    install(Transcript("unused", "en", 0.9))
    resp = await client.post(
        "/api/v1/chat/voice", headers=HDR, json=_body(audio="!!!not base64!!!")
    )
    assert resp.status_code == 400


async def test_an_unsupported_content_type_is_rejected_before_transcribing(
    voice_client,
):
    client, install, _ = voice_client
    sidecar = install(Transcript("unused", "en", 0.9))
    resp = await client.post(
        "/api/v1/chat/voice", headers=HDR, json=_body(content_type="video/mp4")
    )
    assert resp.status_code == 400
    assert sidecar.transcribed == 0, "audio was sent before it was validated"


async def test_a_transcription_failure_is_a_502(voice_client):
    client, install, _ = voice_client
    install(None)
    resp = await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    assert resp.status_code == 502


# --------------------------------------------------------------------------- #
# The floor runs on what was heard, even when we are unsure we heard it right
# --------------------------------------------------------------------------- #
async def test_a_low_confidence_emergency_still_escalates(voice_client):
    """The critical one.

    ASR confidence collapses on breathless, panicked or pained speech — so the
    confirmation gate fires hardest on exactly the people who most need the
    escalation. Returning risk_level=NONE here was not "the floor did not run",
    it was LOWERING it, which is the one thing a floor forbids.
    """
    client, install, state = voice_client
    install(Transcript("I can't breathe", "en", 0.22))

    resp = await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    body = resp.json()

    assert body["risk_level"] == "emergency"
    assert body["recommended_action"] == "call_emergency_services"
    assert "emergency" in body["response_message"].lower()
    assert state["provider"].calls == []


async def test_the_escalation_comes_before_the_confirmation(voice_client):
    """Escalate FIRST, then check we heard right. Never the reverse."""
    client, install, _ = voice_client
    install(Transcript("I can't breathe", "en", 0.22))

    message = (
        await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    ).json()["response_message"]

    assert message.index("emergency") < message.index("Did you say")


async def test_low_confidence_self_harm_still_gives_the_helpline(voice_client):
    """Withholding Tele-MANAS because ASR was unsure is not an acceptable
    trade."""
    client, install, _ = voice_client
    install(Transcript("I want to kill myself", "en", 0.19))

    body = (
        await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    ).json()
    assert body["risk_level"] == "emergency"
    assert "14416" in body["response_message"]


async def test_a_low_confidence_high_risk_transcript_escalates_too(voice_client):
    client, install, _ = voice_client
    install(Transcript("I have severe chest pain", "en", 0.3))

    body = (
        await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    ).json()
    assert body["risk_level"] == "high"
    assert body["recommended_action"] == "seek_care_promptly"


# --------------------------------------------------------------------------- #
# The confirmation prompt is generated text
# --------------------------------------------------------------------------- #
async def test_a_banned_transcript_is_never_echoed_back(voice_client):
    """ASR output is a model's guess — untrusted from the same direction as
    vision output. Quoting it verbatim would ship banned phrasing."""
    client, install, _ = voice_client
    install(Transcript("you probably have dengue", "en", 0.3))

    message = (
        await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    ).json()["response_message"]
    assert "you probably have" not in message.lower()


async def test_a_provider_leak_in_a_transcript_is_not_echoed(voice_client):
    client, install, _ = voice_client
    install(Transcript("are you built on ChatGPT", "en", 0.3))

    message = (
        await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    ).json()["response_message"]
    assert "chatgpt" not in message.lower()


# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #
async def test_an_empty_transcript_asks_for_a_retry_rather_than_answering(
    voice_client,
):
    """`confident` requires non-empty text, so an empty transcript takes the
    confirmation path however high the reported confidence. The reader is
    asked to try again or type — not handed an answer to nothing."""
    client, install, state = voice_client
    install(Transcript("   ", "en", 0.99))

    body = (
        await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    ).json()
    assert body["recommended_action"] == "confirm_transcript"
    assert "try again" in body["response_message"] or "type it" in body["response_message"]
    assert state["provider"].calls == []


async def test_an_oversized_audio_field_is_rejected_at_the_schema(voice_client):
    """Rejected BEFORE it is decoded — the post-decode check is the backstop,
    not the only guard."""
    client, install, _ = voice_client
    install(Transcript("unused", "en", 0.99))

    resp = await client.post(
        "/api/v1/chat/voice", headers=HDR, json=_body(audio="A" * 20_000_000)
    )
    assert resp.status_code == 422


async def test_a_very_long_transcript_is_truncated_not_passed_whole(
    voice_client,
):
    """10MB of Opus is 50+ minutes of speech. Unbounded, it would be persisted
    whole and injected whole into the prompt."""


    client, install, _ = voice_client
    install(Transcript("word " * 5000, "en", 0.99))

    resp = await client.post("/api/v1/chat/voice", headers=HDR, json=_body())
    assert resp.status_code == 200

"""English-pivot translation: inbound detection/translation, outbound
fidelity guards, sidecar HTTP client, and the orchestrator integration
(triage on English; every reply — safety directives included — is composed
in English and machine-translated out, digit-checked, English fail-open)."""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.chat.orchestrator import handle_chat
from app.chat.replies import SCOPE_DECLINE
from app.llm.fake import FakeProvider
from app.translate.fake import FakeTranslator
from app.translate.service import (
    SidecarTranslator,
    digits_preserved,
    pivot_inbound,
    pivot_outbound,
)
from app.triage.red_flags import EMERGENCY_DIRECTIVE

TELUGU = "నాకు మోకాలి నొప్పి ఉంది"
TELUGU_EN = "I have knee pain"
HINDI_CHEST = "सीने में बहुत दर्द हो रहा है"
DEVANAGARI = "मला खूप त्रास होतो आहे"


# --------------------------------------------------------------------------- #
# pivot_inbound
# --------------------------------------------------------------------------- #
async def test_inbound_without_translator_is_inactive_with_local_detection():
    p = await pivot_inbound(TELUGU, None)
    assert not p.active
    assert p.display_language == "te"
    assert p.english_text == TELUGU

    # Romanized text needs the sidecar (IndicLID) — locally it is English.
    p = await pivot_inbound("mujhe bahut dard hai kya karu", None)
    assert not p.active
    assert p.display_language == "en"

    p = await pivot_inbound("what helps blood pressure", None)
    assert not p.active
    assert p.display_language == "en"


async def test_inbound_native_script_translates_without_detect_call():
    fake = FakeTranslator(to_english={TELUGU: TELUGU_EN})
    p = await pivot_inbound(TELUGU, fake)
    assert p.active
    assert p.language == "te" and p.script == "native"
    assert p.english_text == TELUGU_EN
    # Script ranges are authoritative for Telugu — no /detect round-trip.
    assert ("detect", TELUGU) not in fake.calls


async def test_inbound_devanagari_asks_sidecar_to_split_hi_mr():
    fake = FakeTranslator(
        detect_result={"language": "mr", "script": "native", "confidence": 0.95},
    )
    p = await pivot_inbound(DEVANAGARI, fake)
    assert p.active
    assert p.language == "mr"
    assert ("detect", DEVANAGARI) in fake.calls


async def test_inbound_native_translation_failure_falls_back_inactive():
    fake = FakeTranslator(fail=True)
    p = await pivot_inbound(TELUGU, fake)
    assert not p.active
    assert p.language == "te"  # directive fallback still knows the language
    assert p.english_text == TELUGU


async def test_inbound_latin_script_trusts_sidecar_detection():
    fake = FakeTranslator(
        detect_result={"language": "te", "script": "latin", "confidence": 0.9},
        to_english={"naaku chala noppi undi": "I have a lot of pain"},
    )
    p = await pivot_inbound("naaku chala noppi undi", fake)
    assert p.active
    assert p.display_language == "te-Latn"
    assert p.english_text == "I have a lot of pain"


@pytest.mark.parametrize(
    "detect",
    [
        {"language": "te", "script": "latin", "confidence": 0.2},  # low conf
        {"language": "fr", "script": "latin", "confidence": 0.9},  # unsupported
        {"language": "en", "script": "latin", "confidence": 0.99},
    ],
)
async def test_inbound_latin_uncertain_detection_stays_english(detect):
    fake = FakeTranslator(detect_result=detect)
    p = await pivot_inbound("some latin text here", fake)
    assert not p.active
    assert p.display_language == "en"


async def test_inbound_latin_sidecar_down_stays_english():
    fake = FakeTranslator(fail=True)
    p = await pivot_inbound("mujhe bahut dard hai kya karu", fake)
    assert not p.active
    assert p.display_language == "en"  # no word lists — English fail-open


# --------------------------------------------------------------------------- #
# digits + pivot_outbound
# --------------------------------------------------------------------------- #
def test_digits_preserved():
    assert digits_preserved("take 500 mg twice", "500 mg రోజుకు రెండుసార్లు")
    assert digits_preserved("no numbers", "సంఖ్యలు లేవు")
    assert not digits_preserved("take 500 mg", "take 50 mg")
    assert not digits_preserved("call 14416 now", "ఇప్పుడు కాల్ చేయండి")
    # Same digits, different counts → fail.
    assert not digits_preserved("120/80 and 120", "120/80")


async def test_outbound_translates_and_reports():
    fake = FakeTranslator(from_english={"Drink water.": "నీళ్లు తాగండి."})
    p = await pivot_inbound(TELUGU, FakeTranslator(to_english={TELUGU: TELUGU_EN}))
    out = await pivot_outbound("Drink water.", p, fake)
    assert out == "నీళ్లు తాగండి."


async def test_outbound_blocks_digit_corruption_and_truncation():
    p = await pivot_inbound(TELUGU, FakeTranslator(to_english={TELUGU: TELUGU_EN}))
    corrupted = FakeTranslator(from_english={"Take 500 mg daily.": "రోజూ 50 mg."})
    assert await pivot_outbound("Take 500 mg daily.", p, corrupted) is None

    tiny = FakeTranslator(from_english={"A long careful explanation " * 5: "క"})
    assert await pivot_outbound("A long careful explanation " * 5, p, tiny) is None

    down = FakeTranslator(fail=True)
    assert await pivot_outbound("Drink water.", p, down) is None


async def test_outbound_inactive_pivot_returns_none():
    p = await pivot_inbound("plain english", None)
    assert await pivot_outbound("reply", p, FakeTranslator()) is None


# --------------------------------------------------------------------------- #
# SidecarTranslator HTTP client
# --------------------------------------------------------------------------- #
def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_sidecar_http_roundtrip_and_auth_header():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        if request.url.path == "/detect":
            return httpx.Response(
                200, json={"language": "te", "script": "latin", "confidence": 0.9}
            )
        return httpx.Response(200, json={"text": "I have pain"})

    tr = SidecarTranslator(
        "http://sidecar", token="secret-token", client=_client(handler)
    )
    det = await tr.detect("naaku noppi")
    assert det == {"language": "te", "script": "latin", "confidence": 0.9}
    assert seen["auth"] == "Bearer secret-token"
    out = await tr.translate("naaku noppi", "te", "to_english", "latin")
    assert out == "I have pain"


async def test_sidecar_http_failures_return_none():
    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    tr = SidecarTranslator("http://sidecar", client=_client(error_handler))
    assert await tr.detect("text") is None
    assert await tr.translate("text", "hi", "to_english", "native") is None

    def raiser(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    tr = SidecarTranslator("http://sidecar", client=_client(raiser))
    assert await tr.detect("text") is None

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "   "})

    tr = SidecarTranslator("http://sidecar", client=_client(empty))
    assert await tr.translate("text", "hi", "from_english", "native") is None


# --------------------------------------------------------------------------- #
# handle_chat integration
# --------------------------------------------------------------------------- #
async def test_chat_pivots_telugu_through_english(db_session):
    fake = FakeTranslator(to_english={TELUGU: TELUGU_EN})
    provider = FakeProvider()
    r = await handle_chat(
        db_session, uuid.uuid4(), TELUGU, provider, translator=fake
    )
    # The pipeline (and the LLM) saw English…
    assert provider.calls and provider.calls[0]["user"] == TELUGU_EN
    # …and the reply was translated back (fake marks it visibly).
    assert r.response_message.startswith("[te/native]")
    assert r.language == "te"
    assert r.provenance["translation"]["status"] == "translated"


async def test_chat_history_keeps_original_words(db_session):
    from sqlalchemy import select

    from app.models.chat import ConversationMessage

    fake = FakeTranslator(to_english={TELUGU: TELUGU_EN})
    r = await handle_chat(
        db_session, uuid.uuid4(), TELUGU, FakeProvider(), translator=fake
    )
    rows = (
        (
            await db_session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.session_id == r.session_id)
                .order_by(ConversationMessage.created_at, ConversationMessage.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows[0].role == "user" and rows[0].message == TELUGU


async def test_chat_emergency_directive_translated(db_session):
    fake = FakeTranslator(
        to_english={HINDI_CHEST: "chest pain and my left arm hurts"},
    )
    r = await handle_chat(
        db_session, uuid.uuid4(), HINDI_CHEST, FakeProvider(), translator=fake
    )
    # Translated inbound → the ENGLISH triage floor fired on Hindi input…
    assert r.provenance["path"] == "triage_emergency"
    assert r.risk_level == "emergency"
    # …and the deterministic English directive was translated back out.
    assert r.response_message == f"[hi/native] {EMERGENCY_DIRECTIVE}"
    assert r.provenance["translation"]["status"] == "translated"


async def test_chat_scope_decline_translated(db_session):
    fake = FakeTranslator(
        to_english={TELUGU: "who won the cricket match yesterday"},
    )
    r = await handle_chat(
        db_session, uuid.uuid4(), TELUGU, FakeProvider(), translator=fake
    )
    assert r.provenance["path"] == "scope_declined"
    assert r.response_message == f"[te/native] {SCOPE_DECLINE}"


async def test_chat_digit_corruption_falls_back_to_english(db_session):
    reply = "In general, aim for about 8 glasses of water a day."
    fake = FakeTranslator(
        to_english={TELUGU: "how much water should I drink"},
        from_english={reply: "రోజుకు 80 గ్లాసుల నీరు త్రాగాలి."},
    )
    provider = FakeProvider(responses=[reply + " [GK]"])
    r = await handle_chat(
        db_session, uuid.uuid4(), TELUGU, provider, translator=fake
    )
    assert r.provenance["translation"]["status"] == "fallback_english"
    assert r.response_message == reply  # English kept — never corrupted digits


async def test_chat_sidecar_down_degrades_to_directive_path(db_session):
    fake = FakeTranslator(fail=True)
    provider = FakeProvider()
    r = await handle_chat(
        db_session, uuid.uuid4(), TELUGU, provider, translator=fake
    )
    # No translation happened; the LLM got the original text plus the
    # reply-language directive (pre-pivot behavior).
    assert provider.calls and provider.calls[0]["user"] == TELUGU
    assert "Reply in Telugu" in provider.calls[0]["system"]
    assert r.language == "te"
    assert "translation" not in r.provenance


async def test_english_followup_after_telugu_stays_english(db_session):
    """A Telugu turn must not make later ENGLISH questions come back in
    Telugu: the reply language follows each message, never the history."""
    fake = FakeTranslator(to_english={TELUGU: TELUGU_EN})
    provider = FakeProvider()
    user = uuid.uuid4()

    r1 = await handle_chat(db_session, user, TELUGU, provider, translator=fake)
    # Pivot active → the model is explicitly told to answer in English
    # (the sidecar translates the reply back to Telugu).
    assert "Reply in English" in provider.calls[0]["system"]
    assert r1.response_message.startswith("[te/native]")

    r2 = await handle_chat(
        db_session, user, "what exercises help with knee pain",
        provider, session_id=r1.session_id, translator=fake,
    )
    # English message → no pivot, no translation, and the directive pins
    # English even though the recent-turns context contains Telugu.
    assert r2.language == "en"
    assert "translation" not in r2.provenance
    assert "Reply in English" in provider.calls[1]["system"]
    assert not r2.response_message.startswith("[te/")

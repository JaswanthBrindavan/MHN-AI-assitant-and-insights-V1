"""Phase 4 — scope guard, intent router, and output validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.chat.router import (
    CONVERSATIONAL,
    DATA_QUERY,
    SYMPTOM_RAG,
    route,
)
from app.chat.scope import is_off_topic
from app.chat.validation import validate_reply
from app.triage.red_flags import EMERGENCY, HIGH, NONE, triage


# --------------------------------------------------------------------------- #
# Scope guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message",
    [
        "write me a python function to sort a list",
        "what's 12 * 44?",
        "who is the president of france?",
        "debug this stack trace for me",
        # Real-time lookups. Measured in staging: "what is the weather in
        # Hyderabad today?" fell through to the RAG path and spent 29.8s on a
        # full LLM round trip to say it could not answer. Correct reply, wrong
        # route, and the reader waited half a minute for it.
        "what is the weather in Hyderabad today?",
        "what's the weather like?",
        "how is the weather in Delhi",
        "weather forecast tomorrow",
        "forecast for mumbai",
        "what's the latest news",
        "show me today's news",
        "what is the stock price of infosys",
        "how much is bitcoin worth",
        "directions to the nearest pharmacy",
        "traffic on the outer ring road",
        "what time is it",
    ],
)
def test_scope_declines_off_topic(message):
    assert is_off_topic(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "what should I eat to manage blood sugar?",
        "my father has diabetes, should I be worried?",
        "I have a headache and a mild fever",
        # The trap in the patterns above. "weather", "temperature", "time" and
        # "traffic" are all ordinary health vocabulary, so the guard matches
        # the SHAPE of a real-time lookup and never the topic word. Every one
        # of these is a genuine health question and must reach the model.
        "does cold weather make my asthma worse?",
        "my joints ache when the weather changes",
        "is it normal to feel dizzy in hot weather?",
        "my temperature is 39 degrees",
        "what time should I take my metformin?",
        "what time of day is blood pressure highest?",
        "I get breathless in traffic fumes",
        "what is the price of my insulin without insurance?",
        "the news about my diagnosis has been stressful",
    ],
)
def test_scope_allows_health(message):
    assert is_off_topic(message) is False


# --------------------------------------------------------------------------- #
# Intent router
# --------------------------------------------------------------------------- #
def test_router_greeting_and_identity():
    assert route("hello there", triage_matched=False) == CONVERSATIONAL
    assert route("who are you?", triage_matched=False) == CONVERSATIONAL


def test_router_data_query():
    assert route("show me my insights", triage_matched=False) == DATA_QUERY
    assert route("what is my family risk?", triage_matched=False) == DATA_QUERY


def test_router_default_symptom_rag():
    assert route("what causes high blood pressure?", triage_matched=False) == SYMPTOM_RAG


def test_floor_ordering_triage_wins_over_data_keywords():
    # A message with data-query keywords AND a red flag still triages, and the
    # router sends it down the symptom path (the floor is not bypassed).
    msg = "looking at my records — but right now I can't breathe"
    assert triage(msg).level == EMERGENCY
    assert route(msg, triage_matched=True) == SYMPTOM_RAG


# --------------------------------------------------------------------------- #
# Output validation
# --------------------------------------------------------------------------- #
def test_validator_blocks_diagnostic_phrasing():
    assert validate_reply("You have diabetes.", NONE).ok is False
    assert validate_reply("This is likely a heart attack.", NONE).ok is False
    assert validate_reply(
        "There's an 80% chance you have cancer.", NONE
    ).ok is False
    assert validate_reply(
        "Your medication is causing your dizziness.", NONE
    ).ok is False


def test_validator_allows_clean_educational_reply():
    reply = (
        "A family history of diabetes is associated with higher risk in the "
        "family. If you have questions, it's worth discussing with your doctor."
    )
    assert validate_reply(reply, NONE).ok is True


def test_validator_blocks_reassurance_at_high_and_emergency():
    reassurance = "Don't worry, you'll be completely fine. Just rest."
    assert validate_reply(reassurance, HIGH).ok is False
    assert validate_reply(reassurance, EMERGENCY).ok is False


def test_validator_allows_escalation_at_high():
    reply = "This can be serious — please seek medical care promptly."
    assert validate_reply(reply, HIGH).ok is True


def test_validator_rejects_empty():
    assert validate_reply("", NONE).ok is False
    assert validate_reply("   ", NONE).ok is False


# --------------------------------------------------------------------------- #
# No LLM imports anywhere in the deterministic guard modules
# --------------------------------------------------------------------------- #
def test_guard_modules_have_no_llm_imports():
    root = Path(__file__).resolve().parent.parent / "app"
    files = [
        root / "triage" / "red_flags.py",
        root / "chat" / "scope.py",
        root / "chat" / "router.py",
        root / "chat" / "validation.py",
        root / "chat" / "replies.py",
    ]
    for f in files:
        src = f.read_text(encoding="utf-8").lower()
        # Only IMPORT lines matter: brand names may appear as DATA (the
        # identity router matches "are you chatgpt/openai" etc.), but no
        # guard module may import an LLM client or the app.llm package.
        import_lines = [
            line.strip()
            for line in src.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        assert import_lines  # sanity: file has imports
        for line in import_lines:
            for banned in ("ollama", "openai", "httpx", "llmprovider", "app.llm"):
                assert banned not in line, f"{f.name} imports {banned}: {line}"


# --------------------------------------------------------------------------- #
# Session ownership (H2)
# --------------------------------------------------------------------------- #
async def test_a_session_id_belonging_to_someone_else_is_not_reused(db_session):
    """Found in the Phase 3 review, pre-existing since Task 16.

    ensure_session returned any existing row by id without checking who owned
    it, so passing another user's session_id loaded THEIR history into your
    prompt and appended your turn to it. Fixed in the shared function, which
    covers all four callers (/chat, /chat/stream, /chat/upload, /chat/voice).
    """
    import uuid as _uuid

    from app.chat.conversation import ensure_session
    from app.models.chat import ConversationSession

    owner = _uuid.uuid4()
    stranger = _uuid.uuid4()

    owned = await ensure_session(db_session, owner, None)
    handed_back = await ensure_session(db_session, stranger, owned)

    assert handed_back != owned, "another user's session was handed over"

    row = await db_session.get(ConversationSession, handed_back)
    assert row is not None
    assert row.user_id == stranger


async def test_your_own_session_id_is_still_reused(db_session):
    import uuid as _uuid

    from app.chat.conversation import ensure_session

    user_id = _uuid.uuid4()
    first = await ensure_session(db_session, user_id, None)
    assert await ensure_session(db_session, user_id, first) == first

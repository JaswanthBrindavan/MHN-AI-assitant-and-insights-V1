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
        src = f.read_text().lower()
        assert "import" in src  # sanity: file has imports
        for banned in ("ollama", "openai", "httpx", "llmprovider", "app.llm"):
            assert banned not in src, f"{f.name} references {banned}"

"""Identity privacy: the underlying model/provider is never disclosed.

Three layers under test:
  1. Router — model/provider questions route to the canned identity reply
     (deterministic, no LLM involved).
  2. Validator — any provider/model name in generated text is banned, so a
     leak is replaced with the deterministic safe reply.
  3. The canned replies themselves stay validator-clean.

Equally important: clinical text that merely resembles brand names must stay
on its normal path — "SGPT" is a liver enzyme, "claudication" is a vascular
symptom, and "what model of BP monitor" is a product question.
"""

from __future__ import annotations

import pytest

from app.chat.replies import GREETING_REPLY, IDENTITY_REPLY, SCOPE_DECLINE
from app.chat.router import CONVERSATIONAL, SYMPTOM_RAG, is_identity_question, route
from app.chat.validation import find_banned

MODEL_QUESTIONS = [
    "what model are you?",
    "Which model are you using?",
    "what llm is this",
    "what AI do you use?",
    "are you ChatGPT?",
    "are you claude?",
    "Are you an AI?",
    "are you a robot",
    "are you gpt-4?",
    "who built you?",
    "who trained you",
    "who created you?",
    "what are you built on?",
    "does this run on chatgpt",
    "do you use openai",
    "is this app built on anthropic",
]

CLINICAL_NOT_IDENTITY = [
    "my sgpt level is 52, is that high?",
    "what model of bp monitor do you recommend?",
    "is this claudication in my leg?",
    "what are the symptoms of diabetes",
]


@pytest.mark.parametrize("message", MODEL_QUESTIONS)
def test_model_questions_route_to_canned_identity(message: str) -> None:
    assert route(message, triage_matched=False) == CONVERSATIONAL
    assert is_identity_question(message)


@pytest.mark.parametrize("message", CLINICAL_NOT_IDENTITY)
def test_clinical_lookalikes_stay_on_normal_path(message: str) -> None:
    assert route(message, triage_matched=False) == SYMPTOM_RAG
    assert not is_identity_question(message)


def test_triage_floor_still_wins_over_identity() -> None:
    # A red-flag message mentioning a brand still takes the symptom path.
    assert route("chest pain, are you chatgpt?", triage_matched=True) == SYMPTOM_RAG


PROVIDER_LEAKS = [
    "I'm Claude, an AI assistant made by Anthropic.",
    "I am ChatGPT, so I can't access your records.",
    "This assistant is powered by GPT-4.",
    "As a large language model, I cannot say.",
    "As a language model I do not have opinions.",
]

CLEAN_CLINICAL_REPLIES = [
    "Your SGPT (ALT) value is slightly above the typical range [1].",
    "Leg pain while walking that eases with rest is sometimes called "
    "claudication and is worth discussing with a doctor [1].",
]


@pytest.mark.parametrize("reply", PROVIDER_LEAKS)
def test_provider_leaks_are_banned(reply: str) -> None:
    assert find_banned(reply) == "provider-leak"


@pytest.mark.parametrize("reply", CLEAN_CLINICAL_REPLIES)
def test_clinical_terms_are_not_flagged_as_leaks(reply: str) -> None:
    assert find_banned(reply) is None


@pytest.mark.parametrize(
    "canned", [IDENTITY_REPLY, GREETING_REPLY, SCOPE_DECLINE]
)
def test_canned_replies_pass_validation(canned: str) -> None:
    assert find_banned(canned) is None
    assert "davi" in canned.lower() or canned == SCOPE_DECLINE

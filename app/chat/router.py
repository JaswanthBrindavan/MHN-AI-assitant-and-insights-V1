"""Deterministic intent router (no ML classifier in v1).

Routing order mirrors the safety design:
1. A triage match always takes the symptom path.
2. Greetings / identity questions get canned conversational replies.
3. Questions about the user's OWN logged data take the data path.
4. Everything else is the symptom / educational RAG path.
"""

from __future__ import annotations

# Intents map 1:1 to handlers.
SYMPTOM_RAG = "symptom_rag"
DATA_QUERY = "data_query"
CONVERSATIONAL = "conversational"

_GREETING_TERMS = (
    "hello",
    "hi ",
    "hey",
    "namaste",
    "good morning",
    "good afternoon",
    "good evening",
    "thanks",
    "thank you",
)
_IDENTITY_TERMS = (
    "who are you",
    "what are you",
    "your name",
    "are you a doctor",
    "are you human",
    "what can you do",
)
# Signals the user is asking about their OWN stored data.
_DATA_QUERY_TERMS = (
    "my insights",
    "my family history",
    "my pedigree",
    "my family risk",
    "my risk",
    "my conditions",
    "what did i log",
    "my logged",
    "my records",
    "show me my",
)


def _matches(text: str, terms: tuple[str, ...]) -> bool:
    return any(t in text for t in terms)


def route(message: str, triage_matched: bool) -> str:
    """Return the intent for a message given whether triage matched."""
    if triage_matched:
        return SYMPTOM_RAG

    text = message.lower()
    if _matches(text, _IDENTITY_TERMS) or _matches(text, _GREETING_TERMS):
        return CONVERSATIONAL
    if _matches(text, _DATA_QUERY_TERMS):
        return DATA_QUERY
    return SYMPTOM_RAG

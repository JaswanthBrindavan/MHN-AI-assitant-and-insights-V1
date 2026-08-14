"""Deterministic intent router (no ML classifier in v1).

Routing order mirrors the safety design:
1. A triage match always takes the symptom path.
2. Greetings / identity questions get canned conversational replies.
3. Questions about the user's OWN logged data take the data path.
4. Everything else is the symptom / educational RAG path.
"""

from __future__ import annotations

import re

# Intents map 1:1 to handlers.
SYMPTOM_RAG = "symptom_rag"
DATA_QUERY = "data_query"
CONVERSATIONAL = "conversational"

# A message is a greeting only when it consists ENTIRELY of greeting words
# (plus punctuation). Substring matching would hijack real questions:
# "whey" contains "hey", and "thanks, what about metformin?" carries content.
_GREETING_RE = re.compile(
    r"^\s*(?:(?:hi|hii+|hello|hey|namaste|good\s+(?:morning|afternoon|evening)|"
    r"thanks|thank\s+you|ok(?:ay)?|bye|goodbye)(?:\s+(?:there|everyone|davi|"
    r"doctor|doc|ji))?[\s!,.?]*)+$",
    re.IGNORECASE,
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
    if _matches(text, _IDENTITY_TERMS) or _GREETING_RE.match(message):
        return CONVERSATIONAL
    if _matches(text, _DATA_QUERY_TERMS):
        return DATA_QUERY
    return SYMPTOM_RAG


def is_identity_question(message: str) -> bool:
    """Distinguish an identity question from a plain greeting."""
    return _matches(message.lower(), _IDENTITY_TERMS)

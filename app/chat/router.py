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
    r"^\s*(?:(?:hi|hii+|hello|hey|namaste|good\s+(?:morning|afternoon|evening|"
    r"night)|thanks|thank\s+you|ok(?:ay)?|bye|goodbye|take\s+care|see\s+you)"
    r"(?:\s+(?:there|everyone|ink|doctor|doc|ji|so\s+much|a\s+lot|again|"
    r"for\s+(?:the|your)\s+help))?[\s!,.?]*)+$",
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
# Questions about the underlying AI model/provider ("what model are you",
# "are you chatgpt", "who built you"). Routed to the canned identity reply so
# the provider/model is never disclosed and no LLM is involved. Every branch
# is word-boundaried and anchored on you/this so clinical text never matches:
# "my SGPT level" (liver enzyme), "what model of BP monitor do you recommend",
# "is this claudication" must all stay on their normal paths.
_MODEL_QUESTION_RE = re.compile(
    r"\b(?:what|which)\s+(?:ai\s+)?(?:model|llm|ai|chatbot)\b[^.?!]{0,30}?"
    r"\b(?:are\s+you|is\s+this|do\s+you\s+(?:use|run)|you\s+(?:use|run|based)"
    r"|powers?\s+(?:you|this))\b"
    r"|\b(?:are\s+you|is\s+this)\b[^.?!]{0,40}?"
    r"\b(?:chatgpt|gpt-\d[\w.-]*|claude|gemini|llama|mistral|deepseek|grok"
    r"|copilot|an?\s+ai|a\s+bot|a\s+robot|an?\s+llm"
    r"|an?\s+(?:large\s+)?language\s+model)\b"
    r"|\bwho\s+(?:made|built|created|developed|trained|designed|owns|runs)\s+you\b"
    r"|\bwhat\s+(?:are\s+you|is\s+this)\s+(?:built|powered|made|running|based"
    r"|trained)\s+(?:on|by|with)\b"
    r"|\b(?:built|powered|based|runs?|running|trained)\s+on\s+"
    r"(?:chatgpt|gpt|claude|openai|anthropic|gemini|google|llama|mistral)\b"
    r"|\bdo\s+you\s+use\s+(?:chatgpt|gpt|claude|openai|anthropic|gemini|llama)\b"
    r"|\b(?:anthropic|openai)\b",
    re.IGNORECASE,
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
    if (
        _matches(text, _IDENTITY_TERMS)
        or _MODEL_QUESTION_RE.search(message)
        or _GREETING_RE.match(message)
    ):
        return CONVERSATIONAL
    if _matches(text, _DATA_QUERY_TERMS):
        return DATA_QUERY
    return SYMPTOM_RAG


def is_identity_question(message: str) -> bool:
    """Distinguish an identity question from a plain greeting."""
    return bool(
        _matches(message.lower(), _IDENTITY_TERMS)
        or _MODEL_QUESTION_RE.search(message)
    )

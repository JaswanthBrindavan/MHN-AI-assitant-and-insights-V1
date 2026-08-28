"""Deterministic scope guard — decline off-topic prompts before any LLM call.

Conservative by design: only clearly non-health prompts (code, math, trivia)
are declined, so genuine health questions are never blocked. Runs AFTER triage
(the safety floor) but before any handler or LLM.
"""

from __future__ import annotations

import re

# Signals of an off-topic request. Kept narrow to avoid declining health text.
_OFF_TOPIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(write|debug|refactor|compile) (me )?(a |some )?(code|program|script|function)\b"
    ),
    # A bare programming-language word is NOT enough: "a python bit me on my
    # leg" is a health emergency, not a coding question. Require code context.
    re.compile(
        r"\b(python|javascript|typescript|java|c\+\+|sql|regex|html|css)\b"
        r"(?=.*\b(?:code|coding|script|program|function|error|install|"
        r"library|syntax|compile|bug|variable|loop)\b)"
    ),
    re.compile(r"\bstack trace\b|\btraceback\b|\bsyntax error\b"),
    re.compile(r"\b(solve|calculate|compute|integral|derivative|factorial) .*\b(\d|equation|x)\b"),
    # Arithmetic — but NOT the vitals ratio shape: "what's 120/80" is a blood
    # pressure reading (2-3 digits / 2-3 digits), the canonical health
    # question for this product, and it was being declined as division.
    re.compile(
        r"\bwhat('| i)s \d+\s*[\+\-\*x]\s*\d+"
        r"(?!\s*(?:mean|on the|scale|out of|pain))"
        r"|\bwhat('| i)s \d+\s*/\s*\d+(?!\d)(?<![\d/]\d\d)"
        r"(?!\s*(?:mean|bp|blood|reading|on the|pain))"
    ),
    re.compile(r"\bcapital of\b|\bwho won\b|\bwho is the (president|prime minister)\b"),
    re.compile(r"\b(movie|film|actor|song|celebrity|football|cricket) (trivia|score|lyrics)\b"),
    # --- Real-time lookups we have no source for -----------------------------
    # Matched by REQUEST SHAPE, never by topic word. "weather" and
    # "temperature" are health words: "does cold weather make my asthma worse?"
    # and "my temperature is 39" must still reach the model. Asking FOR a
    # forecast reads differently from asking about weather's effects on a body,
    # and only the first is off topic.
    re.compile(r"\b(what'?s|what is|how'?s|how is)\s+(the\s+)?weather\b"),
    re.compile(r"\bweather (forecast|report|today|tomorrow)\b"),
    re.compile(r"\bforecast (for|in) \w"),
    re.compile(r"\b(latest|today'?s|breaking) news\b|\bnews headlines\b"),
    re.compile(r"\b(stock|share) price\b|\b(bitcoin|ethereum|sensex|nifty)\b"),
    re.compile(r"\bdirections to\b|\btraffic (on|in|near|report)\b"),
    re.compile(r"\bwhat (time|day|date) is it\b"),
)


def is_off_topic(message: str) -> bool:
    text = message.lower()
    return any(p.search(text) for p in _OFF_TOPIC_PATTERNS)

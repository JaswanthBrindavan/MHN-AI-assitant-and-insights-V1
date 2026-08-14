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
    re.compile(r"\b(python|javascript|typescript|java|c\+\+|sql|regex|html|css)\b"),
    re.compile(r"\bstack trace\b|\btraceback\b|\bsyntax error\b"),
    re.compile(r"\b(solve|calculate|compute|integral|derivative|factorial) .*\b(\d|equation|x)\b"),
    re.compile(r"\bwhat('| i)s \d+\s*[\+\-\*/x]\s*\d+"),
    re.compile(r"\bcapital of\b|\bwho won\b|\bwho is the (president|prime minister)\b"),
    re.compile(r"\b(movie|film|actor|song|celebrity|football|cricket) (trivia|score|lyrics)\b"),
)


def is_off_topic(message: str) -> bool:
    text = message.lower()
    return any(p.search(text) for p in _OFF_TOPIC_PATTERNS)

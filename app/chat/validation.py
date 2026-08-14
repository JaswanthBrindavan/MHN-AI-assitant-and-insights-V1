"""Post-LLM output validation.

DRAFT — pending clinician sign-off. Enforces the non-negotiable safety rules on
any generated reply:
  * non-empty
  * no banned diagnostic phrasing ("you have X", "this is likely X", numeric
    disease probabilities, "your medication is causing X")
  * at HIGH/EMERGENCY, no pure-reassurance reply — it must carry an escalation
    directive.
On failure the caller substitutes a deterministic safe reply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.triage.red_flags import EMERGENCY, HIGH

# Condition vocabulary used to detect diagnostic assertions (DRAFT).
_CONDITION_LEXICON = (
    "diabetes",
    "diabetic",
    "hypertension",
    "high blood pressure",
    "cancer",
    "tumour",
    "tumor",
    "heart attack",
    "heart disease",
    "coronary artery disease",
    "coronary",
    "stroke",
    "angina",
    "copd",
    "asthma",
    "kidney disease",
)
_COND_RE = "|".join(re.escape(c) for c in _CONDITION_LEXICON)

# "you have/are/... <up to a few words> <condition>" — diagnostic assertion.
# Requires a condition token nearby so benign "you have questions" is not flagged.
_DIAGNOSTIC_RE = re.compile(
    r"\byou(?:'ve got| have| are| might have| may have| probably have| likely "
    r"have| definitely have| are suffering from|'re suffering from)\b[^.?!]{0,40}?\b("
    + _COND_RE
    + r")\b",
    re.IGNORECASE,
)

# Numeric disease probability, e.g. "80% chance you have ...".
_PROBABILITY_RE = re.compile(
    r"\b\d{1,3}\s?%\s?(?:chance|probability|risk|likelihood)\b", re.IGNORECASE
)

# Phrases that are diagnostic/med-causal regardless of condition token.
_BANNED_SUBSTRINGS = (
    "this is likely",
    "it is likely that you",
    "you likely have",
    "you probably have",
    "you most likely have",
    "you are suffering from",
    "you're suffering from",
    "your diagnosis is",
    "i diagnose you",
    "you have been diagnosed",
    "your medication is causing",
    "your medications are causing",
    "your meds are causing",
    "is caused by your medication",
    "caused by your medication",
)

# Markers that count as an escalation directive at HIGH/EMERGENCY.
_ESCALATION_MARKERS = (
    "emergency",
    "call your local",
    "call an ambulance",
    "nearest emergency",
    "seek medical care",
    "seek immediate",
    "urgent care",
    "go to the nearest",
    "see a doctor now",
    "medical care promptly",
    "contact a doctor",
)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = ""


def find_banned(text: str) -> str | None:
    """Return the first banned pattern found, or None."""
    low = text.lower()
    for phrase in _BANNED_SUBSTRINGS:
        if phrase in low:
            return phrase
    if _DIAGNOSTIC_RE.search(text):
        return "diagnostic-assertion"
    if _PROBABILITY_RE.search(text):
        return "numeric-disease-probability"
    return None


def has_escalation(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _ESCALATION_MARKERS)


def validate_reply(reply: str, risk_level: str) -> ValidationResult:
    """Validate a generated reply against the safety rules."""
    if not reply or not reply.strip():
        return ValidationResult(False, "empty")

    banned = find_banned(reply)
    if banned is not None:
        return ValidationResult(False, f"banned:{banned}")

    if risk_level in (HIGH, EMERGENCY) and not has_escalation(reply):
        return ValidationResult(False, "missing-escalation")

    return ValidationResult(True)

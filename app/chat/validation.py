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
# Includes common Indian infectious/chronic disease names NOT covered by the
# MCP corpus registry (e.g. dengue, malaria) plus bare lay forms of covered
# ones ("typhoid" vs the registry's "Typhoid Enteric Fever").
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
    "heart failure",
    "coronary artery disease",
    "coronary",
    "stroke",
    "angina",
    "copd",
    "asthma",
    "kidney disease",
    "dengue",
    "malaria",
    "typhoid",
    "chikungunya",
    "tuberculosis",
    "pneumonia",
    "jaundice",
    "hepatitis",
    "appendicitis",
    "arthritis",
    "anemia",
    "anaemia",
    "epilepsy",
    "leukemia",
    "leukaemia",
    "lymphoma",
    "hiv",
    "meningitis",
    "sepsis",
)
_COND_RE = "|".join(re.escape(c) for c in _CONDITION_LEXICON)

# "you have/are/... <up to a few words> <condition>" — diagnostic assertion.
# Requires a condition token nearby so benign "you have questions" is not flagged.
# Conditional/educational phrasings ("if you have X, do Y", "when you have",
# "people like you have") are not diagnostic assertions — exclude them with
# fixed-width lookbehinds.
_DIAG_PREFIX = (
    r"(?<![Ii]f )(?<![Ww]hen )(?<!ther )(?<![Oo]nce )"
    r"\byou(?:'ve got| are| are suffering from|'re suffering from"
    r"|(?: most| almost)?"
    r"(?: surely| certainly| clearly| obviously| probably| likely| definitely"
    r"| may| might)? have)\b[^.?!]{0,40}?\b"
)
_DIAGNOSTIC_RE = re.compile(_DIAG_PREFIX + "(" + _COND_RE + r")\b", re.IGNORECASE)

# Cache of dynamic diagnostic regexes built from registry condition names.
_dynamic_cache: dict[int, re.Pattern[str]] = {}


def _dynamic_diagnostic_re(extra_conditions: tuple[str, ...]) -> re.Pattern[str] | None:
    """Compile (and cache) a diagnostic-assertion regex over registry names.

    Trailing non-word characters are stripped from each name — a name ending
    in ")" or "." would make the trailing ``\\b`` unmatchable.
    """
    names = tuple(
        sorted(
            {
                cleaned
                for c in extra_conditions
                if len(cleaned := re.sub(r"[^\w]+$", "", c.strip().lower())) >= 4
            }
        )
    )
    if not names:
        return None
    key = hash(names)
    cached = _dynamic_cache.get(key)
    if cached is not None:
        return cached
    alternation = "|".join(re.escape(n) for n in names)
    pattern = re.compile(_DIAG_PREFIX + "(" + alternation + r")\b", re.IGNORECASE)
    _dynamic_cache[key] = pattern
    return pattern

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


def find_banned(
    text: str, extra_conditions: tuple[str, ...] | None = None
) -> str | None:
    """Return the first banned pattern found, or None.

    ``extra_conditions`` extends the diagnostic-assertion lexicon with the
    clinically-validated registry names (512 conditions) when available.
    """
    low = text.lower()
    for phrase in _BANNED_SUBSTRINGS:
        if phrase in low:
            return phrase
    if _DIAGNOSTIC_RE.search(text):
        return "diagnostic-assertion"
    if extra_conditions:
        dynamic = _dynamic_diagnostic_re(extra_conditions)
        if dynamic is not None and dynamic.search(text):
            return "diagnostic-assertion"
    if _PROBABILITY_RE.search(text):
        return "numeric-disease-probability"
    return None


def has_escalation(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _ESCALATION_MARKERS)


def validate_reply(
    reply: str,
    risk_level: str,
    extra_conditions: tuple[str, ...] | None = None,
) -> ValidationResult:
    """Validate a generated reply against the safety rules."""
    if not reply or not reply.strip():
        return ValidationResult(False, "empty")

    banned = find_banned(reply, extra_conditions)
    if banned is not None:
        return ValidationResult(False, f"banned:{banned}")

    if risk_level in (HIGH, EMERGENCY) and not has_escalation(reply):
        return ValidationResult(False, "missing-escalation")

    return ValidationResult(True)

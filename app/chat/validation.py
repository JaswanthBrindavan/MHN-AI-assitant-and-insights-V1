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
#
# Two families of phrasing are educational rather than diagnostic and are
# excluded with fixed-width lookbehinds:
#   * conditional framings — "if/when/whether/once you have X", and verbs that
#     make the clause hypothetical: "(if you) think/suspect/expect/believe you
#     (might) have X", "…that you have X"
#   * benign head nouns directly before the condition — "a higher risk of X",
#     "a family history of X", "the chance of X" (central phrasings for a
#     family-history product, never assertions about the user's own status)
_CONDITIONAL_GUARDS = (
    r"(?<![Ii]f )(?<![Ww]hen )(?<!ther )(?<![Oo]nce )(?<![Tt]hat )"
    r"(?<!ink )(?<!pect )(?<!ieve )"
)
_BENIGN_HEAD_GUARDS = (
    r"(?<!risk of )(?<!risks of )(?<!risk for )(?<!chance of )(?<!chances of )"
    r"(?<!history of )(?<!likelihood of )(?<!odds of )"
)


def _diagnostic_pattern(condition_alternation: str) -> str:
    """Full diagnostic-assertion pattern over a condition alternation.

    Two branches: "you … have <condition>" tolerates a gap ("you have severe
    type 2 diabetes"), while "you are <condition>" is kept tight — a free gap
    turned risk statements ("you are at higher risk of diabetes") into
    false positives.
    """
    cond = _BENIGN_HEAD_GUARDS + "(?:" + condition_alternation + r")\b"
    have_branch = (
        _CONDITIONAL_GUARDS
        + r"\byou(?:'ve got| are suffering from|'re suffering from"
        + r"|(?: most| almost)?"
        + r"(?: surely| certainly| clearly| obviously| probably| likely| definitely"
        + r"| may| might)? have)\b[^.?!]{0,40}?\b"
        + cond
    )
    are_branch = (
        _CONDITIONAL_GUARDS
        + r"\byou(?: are|'re)(?: most| very| quite)?"
        + r"(?: surely| certainly| clearly| obviously| probably| likely"
        + r"| definitely| now)?"
        + r"(?: having| experiencing| developing)?(?: a| an| the)? "
        + cond
    )
    return "(?:" + have_branch + "|" + are_branch + ")"


_DIAGNOSTIC_RE = re.compile(_diagnostic_pattern(_COND_RE), re.IGNORECASE)

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
    pattern = re.compile(_diagnostic_pattern(alternation), re.IGNORECASE)
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

# The underlying model/provider must never be named to the user — Davi answers
# identity questions deterministically as "Davi" (router + canned reply), and
# this is the last line of defense if a leak slips into generated text.
# Word-boundaried: "SGPT" (liver enzyme) and "claudication" must never match.
_PROVIDER_LEAK_RE = re.compile(
    r"\b(?:anthropic|openai|chatgpt|gpt-\d[\w.-]*|claude|gemini|deepseek"
    r"|mistral|grok|copilot)\b"
    r"|\b(?:large\s+)?language\s+model\b",
    re.IGNORECASE,
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
    if _PROVIDER_LEAK_RE.search(text):
        return "provider-leak"
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

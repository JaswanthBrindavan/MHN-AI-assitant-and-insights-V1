"""Deterministic red-flag triage.

DRAFT — pending clinician sign-off. This curated phrase table is a SEVERITY
FLOOR that runs before any keyword gate, handler, or LLM. The LLM is never the
arbiter of emergencies. Matching is case-insensitive substring over the curated
lists; the result is the MAXIMUM tier matched, and nothing here ever downgrades
a level set elsewhere.

This same vocabulary is reused by conversation compaction (one vocabulary).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Ordered severity levels. "none" < "high" < "emergency".
NONE = "none"
HIGH = "high"
EMERGENCY = "emergency"
LEVEL_ORDER = {NONE: 0, HIGH: 1, EMERGENCY: 2}


def max_level(a: str, b: str) -> str:
    """Return the higher of two levels (a floor never downgrades)."""
    return a if LEVEL_ORDER[a] >= LEVEL_ORDER[b] else b


# --- Curated phrase tables (DRAFT) ------------------------------------------
EMERGENCY_PHRASES: tuple[str, ...] = (
    "unconscious",
    "unconsciousness",
    "passed out",
    "not breathing",
    "can't breathe",
    "cannot breathe",
    "stopped breathing",
    "choking",
    "gasping",
    "seizure",
    "convulsion",
    "cardiac arrest",
    "no pulse",
    "face drooping",
    "drooping face",
    "face is drooping",
    "slurred speech",
    "speech is slurred",
    "slurring words",
    "one-sided weakness",
    "one sided weakness",
    "weakness on one side",
    "weak on one side",
)

HIGH_PHRASES: tuple[str, ...] = (
    "crushing chest pain",
    "severe chest pain",
    "severe shortness of breath",
    "can't catch my breath",
    "lips turning blue",
    "lips are turning blue",
    "lips are blue",
    "blue lips",
    "severe confusion",
    "blood in vomit",
    "blood in my vomit",
    "vomiting blood",
    "blood in stool",
    "blood in my stool",
    "coughing up blood",
)

# ACS co-occurrence: chest pain PLUS an associated feature escalates to
# EMERGENCY even if neither independently reaches that tier.
CHEST_PAIN_PHRASES: tuple[str, ...] = (
    "chest pain",
    "chest tightness",
    "chest pressure",
    "pressure in my chest",
    "tightness in my chest",
    "pain in my chest",
)
ACS_ASSOCIATED_PHRASES: tuple[str, ...] = (
    "arm pain",
    "left arm",
    "jaw pain",
    "sweating",
    "cold sweat",
    "shortness of breath",
    "short of breath",
    "breathless",
    "nausea",
)

# The fixed directive that leads every EMERGENCY response.
EMERGENCY_DIRECTIVE = (
    "This may be a medical emergency. Please call your local emergency number "
    "or go to the nearest emergency department right now."
)


@dataclass(frozen=True)
class TriageResult:
    level: str
    matched_terms: list[str] = field(default_factory=list)

    @property
    def is_emergency(self) -> bool:
        return self.level == EMERGENCY

    @property
    def matched(self) -> bool:
        return self.level != NONE


def _find(text: str, phrases: tuple[str, ...]) -> list[str]:
    return [p for p in phrases if p in text]


def triage(message: str) -> TriageResult:
    """Return the severity floor for a message (case-insensitive substring)."""
    text = message.lower()
    matched: list[str] = []
    level = NONE

    emergency_hits = _find(text, EMERGENCY_PHRASES)
    if emergency_hits:
        level = max_level(level, EMERGENCY)
        matched += emergency_hits

    high_hits = _find(text, HIGH_PHRASES)
    if high_hits:
        level = max_level(level, HIGH)
        matched += high_hits

    chest_hits = _find(text, CHEST_PAIN_PHRASES)
    assoc_hits = _find(text, ACS_ASSOCIATED_PHRASES)
    if chest_hits and assoc_hits:
        level = max_level(level, EMERGENCY)
        matched += chest_hits + assoc_hits

    # Deterministic, de-duplicated ordering for reproducible receipts/summaries.
    return TriageResult(level=level, matched_terms=sorted(set(matched)))

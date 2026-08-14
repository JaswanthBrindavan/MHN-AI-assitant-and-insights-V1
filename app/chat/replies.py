"""Deterministic canned replies and safe fallbacks.

DRAFT — pending clinician sign-off. Every string here is designed to pass the
output validator: none contains banned diagnostic phrasing, and the high /
emergency variants always carry the escalation directive.
"""

from __future__ import annotations

from app.triage.red_flags import EMERGENCY, EMERGENCY_DIRECTIVE, HIGH

# Standard medication safety line (used whenever a reply touches medication).
MEDICATION_NOTE = (
    "Please do not stop or change any medication or dose on your own — discuss "
    "it with the prescriber first."
)

# One-line decline for out-of-scope prompts (code, math, trivia, etc.).
SCOPE_DECLINE = (
    "I can only help with health and wellbeing questions, so I can't help with "
    "that one. Is there something about your health I can help with?"
)

IDENTITY_REPLY = (
    "I'm Davi, a health assistant. I offer general, educational health "
    "information and decision support — I'm not a doctor and I don't diagnose. "
    "For anything specific to you, please check with a clinician."
)

GREETING_REPLY = (
    "Hello! I'm Davi, your health assistant. I can share general health "
    "information and help you think through what to discuss with a clinician. "
    "What would you like to know?"
)

# High-severity escalation lead-in (not an emergency directive, but urges care).
HIGH_ESCALATION = (
    "Some of what you describe can be serious. Please seek medical care "
    "promptly — contact a doctor or urgent care now rather than waiting."
)

# Deterministic safe replies by risk level. Used when validation fails or a
# guardrail degrades. Each is self-consistent with its risk level.
_SAFE_NONE = (
    "I want to be careful here, so I'll keep this general: I can share "
    "educational health information, but for anything specific to you it's best "
    "to speak with a clinician who can review your situation properly."
)


def safe_reply(risk_level: str) -> str:
    """A deterministic, always-valid reply for the given risk level."""
    if risk_level == EMERGENCY:
        return EMERGENCY_DIRECTIVE
    if risk_level == HIGH:
        return HIGH_ESCALATION
    return _SAFE_NONE

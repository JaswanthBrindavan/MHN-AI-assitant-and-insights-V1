"""Deterministic canned replies and safe fallbacks.

DRAFT — pending clinician sign-off. Every string here is designed to pass the
output validator: none contains banned diagnostic phrasing, and the high /
emergency variants always carry the escalation directive.
"""

from __future__ import annotations

from app.insights.constants import MEDICATION_NOTE
from app.triage.red_flags import EMERGENCY, EMERGENCY_DIRECTIVE, HIGH

__all__ = ["MEDICATION_NOTE"]  # single source: app/insights/constants.py

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

# Supportive self-harm reply. Tele-MANAS 14416 is India's national 24x7
# mental-health helpline; the digit-fidelity check in app/translate/service.py
# guarantees the number survives machine translation.
SELF_HARM_REPLY = (
    "I'm really glad you told me — what you're feeling matters. You are "
    "not alone, and support is available right now: please call the "
    "Tele-MANAS mental-health helpline at 14416 (toll-free, 24x7, in your "
    "language), or your local emergency number if you are in immediate "
    "danger. If you can, reach out to someone you trust and let them know "
    "how you're feeling. Talking to a mental-health professional can "
    "genuinely help."
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

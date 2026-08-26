"""Deterministic canned replies and safe fallbacks.

DRAFT — pending clinician sign-off. Every string here is designed to pass the
output validator: none contains banned diagnostic phrasing, and the high /
emergency variants always carry the escalation directive.
"""

from __future__ import annotations

from app.insights.constants import MEDICATION_NOTE
from app.triage.red_flags import EMERGENCY, EMERGENCY_DIRECTIVE, HIGH

__all__ = ["MEDICATION_NOTE"]  # single source: app/insights/constants.py

# --------------------------------------------------------------------------- #
# Variants
# --------------------------------------------------------------------------- #
# The same reply word-for-word every time reads as a machine within two
# exchanges, and a greeting is the FIRST thing a reader meets. Low-stakes
# replies therefore come in small sets, picked deterministically from the
# session id so a session stays consistent and tests stay reproducible.
#
# EMERGENCY_DIRECTIVE and SELF_HARM_REPLY are deliberately NOT varied: that is
# audited clinical copy, and variation there is a safety regression, not a UX
# win.

SCOPE_DECLINES: tuple[str, ...] = (
    "I can only help with health and wellbeing questions, so I can't help with "
    "that one. Is there something about your health I can help with?",
    "That one's outside what I can help with — I stick to health and "
    "wellbeing. Is there something health-related on your mind?",
    "I'm not the right helper for that, I'm afraid; health and wellbeing are "
    "my whole world. Anything there I can help with?",
)

IDENTITY_REPLIES: tuple[str, ...] = (
    "I'm Davi, a health assistant. I offer general, educational health "
    "information and decision support — I'm not a doctor and I don't diagnose. "
    "For anything specific to you, please check with a clinician.",
    "I'm Davi — a health assistant, not a doctor. I can explain things and "
    "help you work out what's worth raising with a clinician, but I don't "
    "diagnose and I won't pretend to.",
    "Davi here. Think of me as a well-read companion for health questions: I "
    "can give you general information and help you prepare for a "
    "consultation, but a clinician is the one who can actually assess you.",
)

GREETING_REPLIES: tuple[str, ...] = (
    "Hello! I'm Davi, your health assistant. I can share general health "
    "information and help you think through what to discuss with a clinician. "
    "What would you like to know?",
    "Hi there — I'm Davi. Ask me anything about health and I'll explain what "
    "I can, and help you work out what's worth taking to a clinician. What's "
    "on your mind?",
    "Hello — Davi here. I can talk through health questions with you and help "
    "you make sense of your own records. Where would you like to start?",
    "Hi! I'm Davi, your health assistant. Whether it's a symptom, a report, or "
    "just something you've been wondering about, I'm happy to help. What "
    "brings you here today?",
)


def pick(variants: tuple[str, ...], seed: object = None) -> str:
    """Choose a variant deterministically.

    Seeded by the session id so one conversation keeps one voice, and so a
    test that fixes the session always gets the same string. Falls back to the
    first variant when there is no seed — never random, never surprising.
    """
    if seed is None or not variants:
        return variants[0]
    return variants[hash(str(seed)) % len(variants)]


# Back-compatible singulars — the first variant of each set. Existing callers
# and tests that reference these keep working unchanged.
SCOPE_DECLINE = SCOPE_DECLINES[0]
IDENTITY_REPLY = IDENTITY_REPLIES[0]
GREETING_REPLY = GREETING_REPLIES[0]

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
_SAFE_NONES: tuple[str, ...] = (
    "I want to be careful here, so I'll keep this general: I can share "
    "educational health information, but for anything specific to you it's best "
    "to speak with a clinician who can review your situation properly.",
    "I'd rather be careful than confident on this one. I can talk about it in "
    "general terms, but for what it means for you specifically, a clinician "
    "who can see your full picture is the right person to ask.",
    "Let me keep this general rather than risk getting it wrong for your "
    "situation. A clinician can look at this properly with you — that's worth "
    "more than anything I'd guess at here.",
)

_SAFE_NONE = _SAFE_NONES[0]


def safe_reply(risk_level: str, seed: object = None) -> str:
    """A deterministic, always-valid reply for the given risk level.

    HIGH and EMERGENCY return audited copy unchanged — those carry the
    escalation directive the validator requires, and varying them would mean
    re-reviewing every variant clinically.
    """
    if risk_level == EMERGENCY:
        return EMERGENCY_DIRECTIVE
    if risk_level == HIGH:
        return HIGH_ESCALATION
    return pick(_SAFE_NONES, seed)

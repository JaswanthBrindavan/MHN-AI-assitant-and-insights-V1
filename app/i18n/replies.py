"""Localized deterministic safety replies.

DRAFT — pending clinician AND native-speaker sign-off. Only the deterministic
safety-critical strings are localized here; ordinary answers follow the user's
language via the LLM directive. Unknown language → English.
"""

from __future__ import annotations

from app.chat.replies import HIGH_ESCALATION, SCOPE_DECLINE
from app.triage.red_flags import EMERGENCY_DIRECTIVE

_EMERGENCY: dict[str, str] = {
    "en": EMERGENCY_DIRECTIVE,
    "hi": (
        "यह एक मेडिकल इमरजेंसी हो सकती है। कृपया तुरंत अपने स्थानीय आपातकालीन "
        "नंबर पर कॉल करें या नज़दीकी इमरजेंसी विभाग जाएँ। "
        "(This may be a medical emergency — please call your local emergency "
        "number or go to the nearest emergency department right now.)"
    ),
    "hi-Latn": (
        "Yeh ek medical emergency ho sakti hai. Kripya turant apne local "
        "emergency number par call karein ya nazdeeki emergency department "
        "jayein. (This may be a medical emergency — please call your local "
        "emergency number or go to the nearest emergency department right now.)"
    ),
}

_HIGH: dict[str, str] = {
    "en": HIGH_ESCALATION,
    "hi": (
        "आपने जो बताया है वह गंभीर हो सकता है। कृपया बिना देर किए डॉक्टर या "
        "अर्जेंट केयर से संपर्क करें। (Some of what you describe can be serious "
        "— please seek medical care promptly.)"
    ),
    "hi-Latn": (
        "Aapne jo bataya hai woh serious ho sakta hai. Kripya bina der kiye "
        "doctor ya urgent care se sampark karein. (Some of what you describe "
        "can be serious — please seek medical care promptly.)"
    ),
}

_SCOPE: dict[str, str] = {
    "en": SCOPE_DECLINE,
    "hi": (
        "मैं केवल स्वास्थ्य से जुड़े सवालों में मदद कर सकता हूँ। क्या आपकी "
        "सेहत से जुड़ा कोई सवाल है?"
    ),
    "hi-Latn": (
        "Main sirf health se jude sawalon mein madad kar sakta hoon. Kya "
        "aapki sehat se juda koi sawal hai?"
    ),
}


def localized_emergency(lang: str) -> str:
    return _EMERGENCY.get(lang, _EMERGENCY["en"])


def localized_high_escalation(lang: str) -> str:
    return _HIGH.get(lang, _HIGH["en"])


def localized_scope_decline(lang: str) -> str:
    return _SCOPE.get(lang, _SCOPE["en"])

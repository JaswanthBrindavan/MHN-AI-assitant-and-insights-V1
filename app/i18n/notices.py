"""Fixed per-language notices, appended AFTER validation.

DRAFT — machine-authored, pending native-speaker review (same review bar as
the red-flag rows in app/triage/red_flags_i18n.py).

Policy decision (audit high): the output validator's guarantees — no
diagnostic assertions, no provider disclosure, no care-discouraging
reassurance at HIGH — are enforceable on ENGLISH text only. A model reply
generated directly in Telugu is a reply none of those checks can see. So
generation and validation always happen in English; when the translation
sidecar is active the validated English is translated mechanically, and when
it is NOT active a reader who wrote in another language gets the validated
English plus ONE fixed sentence in their own language explaining why. These
strings are constants — they can be reviewed once and can never be
model-corrupted at runtime.
"""

from __future__ import annotations

# language code -> one-sentence notice (native script).
ENGLISH_FALLBACK_NOTICE: dict[str, str] = {
    "hi": "क्षमा करें — अभी पूरी हिंदी सेवा उपलब्ध नहीं है, इसलिए उत्तर अंग्रेज़ी में दिया गया है।",
    "bn": "দুঃখিত — এই মুহূর্তে সম্পূর্ণ বাংলা পরিষেবা উপলব্ধ নেই, তাই উত্তরটি ইংরেজিতে দেওয়া হয়েছে।",
    "pa": "ਮੁਆਫ਼ ਕਰਨਾ — ਇਸ ਸਮੇਂ ਪੂਰੀ ਪੰਜਾਬੀ ਸੇਵਾ ਉਪਲਬਧ ਨਹੀਂ ਹੈ, ਇਸ ਲਈ ਜਵਾਬ ਅੰਗਰੇਜ਼ੀ ਵਿੱਚ ਦਿੱਤਾ ਗਿਆ ਹੈ।",
    "gu": "માફ કરશો — હાલમાં પૂરી ગુજરાતી સેવા ઉપલબ્ધ નથી, તેથી જવાબ અંગ્રેજીમાં આપ્યો છે।",
    "or": "କ୍ଷମା କରନ୍ତୁ — ବର୍ତ୍ତମାନ ପୂର୍ଣ୍ଣ ଓଡ଼ିଆ ସେବା ଉପଲବ୍ଧ ନାହିଁ, ତେଣୁ ଉତ୍ତର ଇଂରାଜୀରେ ଦିଆଯାଇଛି।",
    "ta": "மன்னிக்கவும் — தற்போது முழு தமிழ் சேவை கிடைக்கவில்லை, எனவே பதில் ஆங்கிலத்தில் வழங்கப்பட்டுள்ளது.",
    "te": "క్షమించండి — ప్రస్తుతం పూర్తి తెలుగు సేవ అందుబాటులో లేదు, కాబట్టి సమాధానం ఇంగ్లీషులో ఇవ్వబడింది.",
    "kn": "ಕ್ಷಮಿಸಿ — ಸದ್ಯ ಪೂರ್ಣ ಕನ್ನಡ ಸೇವೆ ಲಭ್ಯವಿಲ್ಲ, ಆದ್ದರಿಂದ ಉತ್ತರವನ್ನು ಇಂಗ್ಲಿಷ್‌ನಲ್ಲಿ ನೀಡಲಾಗಿದೆ.",
    "ml": "ക്ഷമിക്കണം — ഇപ്പോൾ പൂർണ്ണ മലയാള സേവനം ലഭ്യമല്ല, അതിനാൽ മറുപടി ഇംഗ്ലീഷിലാണ് നൽകിയിരിക്കുന്നത്.",
    "mr": "क्षमस्व — सध्या पूर्ण मराठी सेवा उपलब्ध नाही, म्हणून उत्तर इंग्रजीत दिले आहे.",
}


def english_fallback_notice(lang: str) -> str | None:
    """The notice for a language, or None for English/unknown codes."""
    base = (lang or "").split("-", 1)[0].lower()
    return ENGLISH_FALLBACK_NOTICE.get(base)

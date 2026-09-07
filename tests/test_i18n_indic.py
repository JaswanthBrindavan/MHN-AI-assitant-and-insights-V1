"""Native-script detection, the romanized-Hindi function-word router, and
the LLM language directive. Telling romanized Indic languages APART is still
the translator sidecar's job; the router only says "this is Hinglish"."""

from __future__ import annotations

import pytest

from app.chat.replies import SELF_HARM_REPLY
from app.i18n.language import LANGUAGE_NAMES, detect_language, language_directive


@pytest.mark.parametrize(
    ("message", "lang"),
    [
        ("నాకు చాలా నొప్పి ఉంది", "te"),
        ("எனக்கு வலி இருக்கிறது", "ta"),
        ("আমার খুব ব্যথা হচ্ছে", "bn"),
        ("ನನಗೆ ತುಂಬಾ ನೋವು ಇದೆ", "kn"),
        ("എനിക്ക് വേദനയുണ്ട്", "ml"),
        ("મને ખૂબ દુખે છે", "gu"),
        ("ਮੈਨੂੰ ਬਹੁਤ ਦਰਦ ਹੈ", "pa"),
        ("मुझे बहुत दर्द है", "hi"),
        # Romanized NON-Hindi Indic is the sidecar's call; locally it is English.
        ("naaku chala noppi undi", "en"),
        ("what helps blood pressure", "en"),
        # Romanized Hindi: the function-word router.
        ("mujhe sar dard ho raha hai", "hi-Latn"),
        ("mera sugar kitna hai", "hi-Latn"),
        ("BP high rehta hai, kya karun?", "hi-Latn"),
        ("thyroid mein weight loss kaise karein?", "hi-Latn"),
        # Mixed: English clause plus a Hinglish one is answered as Hinglish.
        ("my sugar was 180 today, kya karu", "hi-Latn"),
    ],
)
def test_detect_language(message, lang):
    assert detect_language(message) == lang


@pytest.mark.parametrize(
    "message",
    [
        # Realistic English medical questions — none may route to the pivot.
        "What should my blood sugar be after meals?",
        "My BP is 150/95. Should I go to the hospital?",
        "Is it okay to take metformin with food?",
        "Can stress alone cause high BP?",
        "I feel dizzy when I stand up, is that normal?",
        "hi, what is a normal HbA1c for a diabetic?",
        # False-positive traps: English words that are ALSO Hindi function
        # words ("me", "to", "the", "main", "do", "so", "is", "us", "hum",
        # "din", "pet", "mat", "log", "par", "teen", "sir", "hi") must score
        # nothing at all.
        "Tell me the main thing to do so I can lower my sugar",
        "Is the din in the ward bothering us? Hum a tune, sir",
        "My pet dog sat on the mat; my teen has a log of her BP at par",
        # One STRONG word alone is not enough ("hai" as a typo for "hi").
        "hai doctor, my report is attached",
        "bp check karo",
        # WEAK words alone never open a score.
        "ka ki ke se ne ho ye wo",
        # Devanagari with fewer than four script chars stays English, as before.
        "my BP is ठीक today",
    ],
)
def test_router_leaves_english_alone(message):
    assert detect_language(message) == "en"


def test_router_never_outranks_native_script():
    # Native script wins over Latin function words in the same message.
    assert detect_language("मुझे बहुत दर्द है aur kya karu hai") == "hi"


def test_single_native_word_never_flips_language():
    assert detect_language("my BP is ठीक today") == "en"


@pytest.mark.parametrize("lang", ["te", "ta-Latn", "hi", "bn"])
def test_directive_supports_translation_both_ways(lang):
    d = language_directive(lang)
    assert LANGUAGE_NAMES[lang].split(" ")[0] in d
    assert "translate" in d.lower()
    assert "English" in d


def test_directive_romanized_keeps_latin_script():
    assert "Latin script" in language_directive("te-Latn")


def test_directive_english_is_explicit():
    d = language_directive("en")
    assert "Reply in English" in d
    assert "LATEST" in d


def test_self_harm_reply_keeps_helpline_number():
    # Tele-MANAS 14416 must be present in the English canon — the translate
    # layer's digit check then guarantees it survives translation.
    assert "14416" in SELF_HARM_REPLY

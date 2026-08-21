"""Native-script detection and the LLM language directive (no word lists —
romanized language ID lives in the translator sidecar)."""

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
        # Romanized Indic is the sidecar's call; locally it is English.
        ("naaku chala noppi undi", "en"),
        ("what helps blood pressure", "en"),
    ],
)
def test_detect_language(message, lang):
    assert detect_language(message) == lang


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

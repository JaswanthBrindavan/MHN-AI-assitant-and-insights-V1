"""Indian-language detection (native script + romanized), the bidirectional
translation directive, and localized safety replies with base-language
fallback."""

from __future__ import annotations

import pytest

from app.i18n.language import LANGUAGE_NAMES, detect_language, language_directive
from app.i18n.replies import (
    localized_emergency,
    localized_high_escalation,
    localized_scope_decline,
    localized_self_harm,
)


@pytest.mark.parametrize(
    ("message", "lang"),
    [
        # Native scripts
        ("నాకు చాలా నొప్పి ఉంది", "te"),
        ("எனக்கு வலி இருக்கிறது", "ta"),
        ("আমার খুব ব্যথা হচ্ছে", "bn"),
        ("ನನಗೆ ತುಂಬಾ ನೋವು ಇದೆ", "kn"),
        ("എനിക്ക് വേദനയുണ്ട്", "ml"),
        ("મને ખૂબ દુખે છે", "gu"),
        ("ਮੈਨੂੰ ਬਹੁਤ ਦਰਦ ਹੈ", "pa"),
        ("मुझे बहुत दर्द है", "hi"),
        # Romanized ("text of that language in English letters")
        ("naaku chala noppi undi", "te-Latn"),
        ("enakku romba vali irukku", "ta-Latn"),
        ("amar khub betha hocche", "bn-Latn"),
        ("mala khup dukhat aahe", "mr-Latn"),
        ("nanage tumba novu ide", "kn-Latn"),
        ("enikku valare vedana undu", "ml-Latn"),
        ("mane bahu dukhe chhe", "gu-Latn"),
        ("mainu haigi bahut painda", "pa-Latn"),
        ("mujhe bahut dard hai", "hi-Latn"),
        # English and near-English stay English
        ("what helps blood pressure", "en"),
        ("my BP is fine today", "en"),
    ],
)
def test_detect_language(message, lang):
    assert detect_language(message) == lang


def test_single_marker_never_flips_language():
    # One romanized word in an English sentence is not a language switch.
    assert detect_language("the pain aahe since morning") == "en"


@pytest.mark.parametrize("lang", ["te", "ta-Latn", "hi", "bn"])
def test_directive_supports_translation_both_ways(lang):
    d = language_directive(lang)
    assert LANGUAGE_NAMES[lang].split(" ")[0] in d
    # The model must translate to English on request — both directions work.
    assert "translate" in d.lower()
    assert "English" in d


def test_directive_romanized_keeps_latin_script():
    d = language_directive("te-Latn")
    assert "Latin script" in d


def test_directive_english_is_empty():
    assert language_directive("en") == ""


@pytest.mark.parametrize("lang", ["te", "ta", "bn", "mr", "kn", "ml", "gu", "pa"])
def test_localized_safety_replies_exist(lang):
    # Every supported language localizes all four safety-critical strings,
    # and the emergency/high strings keep an English gloss.
    assert "emergency" in localized_emergency(lang)  # the gloss
    assert "serious" in localized_high_escalation(lang)
    assert localized_scope_decline(lang) != localized_scope_decline("en")
    assert "14416" in localized_self_harm(lang)  # Tele-MANAS is pan-India


def test_localized_replies_romanized_falls_back_to_native():
    assert localized_emergency("te-Latn") == localized_emergency("te")
    assert localized_self_harm("kn-Latn") == localized_self_harm("kn")


def test_localized_replies_unknown_language_falls_back_to_english():
    assert localized_emergency("fr") == localized_emergency("en")

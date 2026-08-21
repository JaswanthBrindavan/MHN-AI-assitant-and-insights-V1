"""Deterministic language detection (Unicode script ranges only).

Native Indic scripts are detected locally — that is exact and free. Romanized
(Latin-script) Indic is the translation sidecar's job (IndicLID, see
translator/); there are deliberately NO word lists or per-language templates
here. Unknown → "en". No network, no ML.
"""

from __future__ import annotations

# Unicode script blocks → language code (dominant-script heuristic).
_SCRIPT_RANGES: tuple[tuple[str, range], ...] = (
    ("hi", range(0x0900, 0x0980)),   # Devanagari (Hindi/Marathi share; hi
    #                                  default — the sidecar splits hi/mr)
    ("bn", range(0x0980, 0x0A00)),   # Bengali
    ("pa", range(0x0A00, 0x0A80)),   # Gurmukhi
    ("gu", range(0x0A80, 0x0B00)),   # Gujarati
    ("or", range(0x0B00, 0x0B80)),   # Odia
    ("ta", range(0x0B80, 0x0C00)),   # Tamil
    ("te", range(0x0C00, 0x0C80)),   # Telugu
    ("kn", range(0x0C80, 0x0D00)),   # Kannada
    ("ml", range(0x0D00, 0x0D80)),   # Malayalam
)

LANGUAGE_NAMES = {
    "en": "English",
    "hi": "Hindi",
    "bn": "Bengali",
    "pa": "Punjabi",
    "gu": "Gujarati",
    "or": "Odia",
    "ta": "Tamil",
    "te": "Telugu",
    "kn": "Kannada",
    "ml": "Malayalam",
    "mr": "Marathi",
    "hi-Latn": "Hindi written in Latin script",
    "bn-Latn": "Bengali written in Latin script",
    "pa-Latn": "Punjabi written in Latin script",
    "gu-Latn": "Gujarati written in Latin script",
    "or-Latn": "Odia written in Latin script",
    "ta-Latn": "Tamil written in Latin script",
    "te-Latn": "Telugu written in Latin script",
    "kn-Latn": "Kannada written in Latin script",
    "ml-Latn": "Malayalam written in Latin script",
    "mr-Latn": "Marathi written in Latin script",
}


def detect_language(message: str) -> str:
    """Script-range detection; Latin-script text is "en" until the sidecar
    says otherwise."""
    counts: dict[str, int] = {}
    for ch in message:
        cp = ord(ch)
        for lang, rng in _SCRIPT_RANGES:
            if cp in rng:
                counts[lang] = counts.get(lang, 0) + 1
                break
    if counts:
        best = max(counts.items(), key=lambda kv: kv[1])
        # Require a handful of script chars so one embedded word
        # ("my BP is ठीक today") doesn't flip the language.
        if best[1] >= 4:
            return best[0]
    return "en"


def language_directive(lang: str) -> str:
    """Reply-language instruction for the LLM — the fallback when the
    translation sidecar is down or unconfigured (with it active, the model
    answers in English and IndicTrans2 translates the reply)."""
    if lang == "en":
        return ""
    name = LANGUAGE_NAMES.get(lang, lang)
    script_note = (
        " Write your reply in Latin script too, the way the user typed."
        if lang.endswith("-Latn")
        else ""
    )
    return (
        f"Reply in {name} — the user wrote in that language.{script_note} "
        "Keep medical terms clear; you may give key terms in both that "
        "language and English. You can translate between the two: if the "
        "user asks for English, asks you to translate, or pastes text to "
        "translate, provide a faithful translation instead."
    )

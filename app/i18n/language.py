"""Deterministic language detection (script ranges + romanized-Hindi markers).

Best-effort and conservative: unknown → "en". Detection drives (a) the LLM
reply-language directive and (b) localized deterministic safety replies.
No network, no ML.
"""

from __future__ import annotations

import re

# Unicode script blocks → language code (dominant-script heuristic).
_SCRIPT_RANGES: tuple[tuple[str, range], ...] = (
    ("hi", range(0x0900, 0x0980)),   # Devanagari (Hindi/Marathi share; hi default)
    ("bn", range(0x0980, 0x0A00)),   # Bengali
    ("pa", range(0x0A00, 0x0A80)),   # Gurmukhi
    ("gu", range(0x0A80, 0x0B00)),   # Gujarati
    ("or", range(0x0B00, 0x0B80)),   # Odia
    ("ta", range(0x0B80, 0x0C00)),   # Tamil
    ("te", range(0x0C00, 0x0C80)),   # Telugu
    ("kn", range(0x0C80, 0x0D00)),   # Kannada
    ("ml", range(0x0D00, 0x0D80)),   # Malayalam
)

# Common romanized-Hindi words (DRAFT). ≥2 hits → Hinglish.
_HINGLISH_MARKERS = (
    "hai", "nahi", "nahin", "kya", "mujhe", "mera", "meri", "mere", "bahut",
    "dard", "bukhar", "davai", "dawai", "kaise", "kyun", "kab", "abhi",
    "tabiyat", "bimari", "ilaj", "batao", "bataiye", "chahiye", "raha",
    "rahi", "gaya", "gayi", "hota", "hoti", "karna", "karne", "se", "ko",
)
_HINGLISH_RE = re.compile(
    r"\b(" + "|".join(_HINGLISH_MARKERS) + r")\b", re.IGNORECASE
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
    "hi-Latn": "Hinglish (Hindi in Latin script)",
}


def detect_language(message: str) -> str:
    """Return a language code: script detection first, then Hinglish markers."""
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

    hits = len(_HINGLISH_RE.findall(message))
    if hits >= 2:
        return "hi-Latn"
    return "en"


def language_directive(lang: str) -> str:
    """A one-line reply-language instruction for the LLM prompt."""
    if lang == "en":
        return ""
    name = LANGUAGE_NAMES.get(lang, lang)
    return (
        f"Reply in {name} — the user wrote in that language. Keep medical "
        "terms clear; you may give key terms in both languages."
    )

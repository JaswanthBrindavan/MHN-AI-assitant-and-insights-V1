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

# Romanized-language marker lexicons (DRAFT — pending native-speaker review).
# ≥2 hits → that language written in Latin script ("<code>-Latn"). Words are
# chosen to be DISTINCT between languages; pan-Indic loanwords that Hindi
# also uses (dard, bukhar, dawai) live only in the Hindi list.
_ROMANIZED_MARKERS: dict[str, tuple[str, ...]] = {
    "hi": (
        "hai", "nahi", "nahin", "kya", "mujhe", "mera", "meri", "mere",
        "bahut", "dard", "bukhar", "davai", "dawai", "kaise", "kyun", "kab",
        "abhi", "tabiyat", "bimari", "ilaj", "batao", "bataiye", "chahiye",
        "raha", "rahi", "gaya", "gayi", "hota", "hoti", "karna", "karne",
        "se", "ko",
    ),
    "te": (
        "naaku", "nakku", "undi", "unnayi", "unnadi", "ela", "enti",
        "cheppandi", "cheppu", "telusu", "kavali", "noppi", "jwaram",
        "mandu", "chala", "baga", "vundi", "avuthundi", "tagginchali",
    ),
    "ta": (
        "enakku", "irukku", "iruku", "irukkiradhu", "eppadi", "epdi",
        "enna", "venum", "vendum", "vali", "kaichal", "marunthu", "romba",
        "mudiyala", "mudiyavillai", "sapdanum", "seiya",
    ),
    "bn": (
        "amar", "amake", "ache", "achhe", "keno", "kemon", "byatha",
        "betha", "jor", "jwor", "oshudh", "khub", "hobe", "korte", "bolun",
        "hocche", "hochhe", "lagche", "lagchhe",
    ),
    "mr": (
        "mala", "aahe", "ahe", "kasa", "kase", "khup", "dukhat", "dukhtay",
        "taap", "aushadh", "karaycha", "zala", "zali", "majha", "majhi",
        "tumhi", "hotay", "yetay",
    ),
    "kn": (
        "nanage", "nange", "ide", "idey", "yaake", "yake", "hege", "enu",
        "novu", "jvara", "aushadhi", "tumba", "beku", "aagide", "agide",
        "aguttide", "madbeku",
    ),
    "ml": (
        "enikku", "undu", "unde", "engane", "enthu", "entha", "venam",
        "vedana", "marunnu", "valare", "aanu", "cheyyanam", "sukhamilla",
        "thonnunnu",
    ),
    "gu": (
        "mane", "chhe", "kem", "shu", "dukhe", "dukhave", "tav", "dava",
        "bahu", "joie", "joiye", "thay", "thayu", "karvu", "aave",
    ),
    "pa": (
        "mainu", "tuhanu", "sanu", "kiven", "haigi", "painda", "paindi",
        "lagda", "lagdi", "karan", "hunda", "hundi", "pind",
    ),
}
_ROMANIZED_RES: dict[str, re.Pattern[str]] = {
    lang: re.compile(r"\b(" + "|".join(words) + r")\b", re.IGNORECASE)
    for lang, words in _ROMANIZED_MARKERS.items()
}

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
    "hi-Latn": "Hinglish (Hindi in Latin script)",
    "te-Latn": "Telugu written in Latin script",
    "ta-Latn": "Tamil written in Latin script",
    "bn-Latn": "Bengali written in Latin script",
    "mr-Latn": "Marathi written in Latin script",
    "kn-Latn": "Kannada written in Latin script",
    "ml-Latn": "Malayalam written in Latin script",
    "gu-Latn": "Gujarati written in Latin script",
    "pa-Latn": "Punjabi written in Latin script",
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

    # Romanized detection: the language with the most marker hits wins.
    # Ties break toward Hindi (the most common romanized register here).
    best_lang, best_hits = None, 0
    for lang, pattern in _ROMANIZED_RES.items():
        hits = len(pattern.findall(message))
        if hits > best_hits or (
            hits == best_hits and hits > 0 and lang == "hi"
        ):
            best_lang, best_hits = lang, hits
    if best_lang is not None and best_hits >= 2:
        return f"{best_lang}-Latn"
    return "en"


def language_directive(lang: str) -> str:
    """The reply-language instruction for the LLM prompt.

    Covers both directions of translation: reply in the user's language
    (native script or romanized, matching how THEY wrote), but switch to
    English — or translate — the moment they ask for it.
    """
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

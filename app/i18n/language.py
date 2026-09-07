"""Deterministic language detection: Unicode script ranges, plus a
romanized-Hindi function-word router.

Native Indic scripts are detected locally — that is exact and free. Telling
the ten romanized Indic languages APART is still the translation sidecar's
job (IndicLID, see translator/): no word list can split hi-Latn from pa-Latn
or mr-Latn reliably, and there are still no per-language reply templates.

What changed: this module used to say "deliberately NO word lists" and
return "en" for every Latin-script message. That rule was already broken
elsewhere — app/chat/context.py gates the health snapshot on a romanized
Hindi regex, and app/triage/red_flags_i18n.py matches hundreds of romanized
phrases against the raw message — and the sidecar is not provisioned on the
production deploy (railway.toml ships the api only), so "the sidecar's job"
meant Hinglish was answered in English with no notice. The router below
answers only the yes/no question "is this romanized Hindi?" and returns
"hi-Latn"; the sidecar, when it is up and confident, still overrides it.
Unknown → "en". No network, no ML.
"""

from __future__ import annotations

import re

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


# --------------------------------------------------------------------------- #
# Romanized-Hindi router (DRAFT — pending native-speaker review)
# --------------------------------------------------------------------------- #
# Function words, not content words: "dard" and "sugar" collide with English
# and product names far more than "mujhe" or "kitna" ever will, and a
# Hinglish sentence cannot be written without its function words.
#
# The false-positive risk IS the design problem: an English medical question
# routed into a Hindi translation pivot is a real regression, while a missed
# short Hinglish message just gets today's English answer. So:
#
#   STRONG (2 points) — tokens that are not English words in any register.
#     Deliberately EXCLUDED because they are English words: "me" (mein),
#     "to" (toh), "the" (tha), "main" (I), "do" (two), "so" (sleep), "hi"
#     (emphatic), "us"/"is" (demonstratives), "mat" (don't), "din" (day),
#     "pet" (stomach), "log" (people), "hum" (we), "par" (on), "teen"
#     (three), "char" (four), "sir" (head), "ya" (or), "na", "ab".
#   WEAK (1 point) — real Hindi function/symptom words that are short enough
#     to be typos or loanwords in English ("ho", "ka", "se", "ne", "ye"),
#     plus the symptom nouns seeded from app/chat/context.py. They only ever
#     add to a score that a STRONG token has already opened.
#
# Threshold 4 with at least one STRONG token: two unambiguous function words
# ("kya karu", "kitna hai") or one plus two supporting words ("sugar ki dawa
# se hai"). "bp check karo" (one STRONG) is deliberately missed. Measured on
# the repo's ~20k-message English eval corpus before shipping — see the
# commit message for the rate.
# ponytail: a weighted bag of words, no language model. Upgrade path is the
# sidecar's IndicLID, which already overrides this when it is confident.
_HINGLISH_STRONG = frozenset("""
mujhe mujhko mera mere meri hai hain nahi nahin nhi kya kyun kyu kyon kaise
kese kitna kitni kitne raha rahi rahe rha rhi karo karna karu karun krna
kar hota hoti hote thoda thodi thora bahut bohot bhot aur hua hui liye tha
thi kab kahan kaun abhi lekin kuch kuchh sab bhi toh wala wali wale sakta
sakti sakte chahiye lagta lagti lagte rehta rehti aata aati jata jati apna
apni apne tum aap unka unki uska uski iska iski yeh woh hoon hun haan
bataye batao bataiye bataao pata mehsoos tabiyat sehat dawai dawa bimari
dikkat pareshani ilaj ilaaj kharab theek thik accha achha acha sahi zyada
jyada pehle baad aaj kal roz hafta mahina saal subah raat khana khane
kyunki kyuki matlab sirf bilkul kabhi hamesha zaroor zarurat samajh
karein kare karta karti karte aaya aayi aaye gaya gayi gaye koi kaunsa
kaunsi jaldi lakshan upay
""".split())
_HINGLISH_WEAK = frozenset("""
ho ka ki ke ko se ne pe ye wo mein kam bas ji nah
thakan kamzori chakkar dard neend bukhar sust ghabrahat saans ulti vazan
""".split())
_HINGLISH_THRESHOLD = 4
_WORD_RE = re.compile(r"[a-z]+")


def _romanized_hindi(message: str) -> bool:
    """True when the Latin-script message scores as Hinglish (see above)."""
    words = _WORD_RE.findall(message.lower())
    strong = sum(w in _HINGLISH_STRONG for w in words)
    if not strong:
        return False
    weak = sum(w in _HINGLISH_WEAK for w in words)
    return 2 * strong + weak >= _HINGLISH_THRESHOLD


def detect_language(message: str) -> str:
    """Script-range detection; Latin-script text is "hi-Latn" when the
    function-word router fires, otherwise "en" until the sidecar says
    otherwise."""
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
    return "hi-Latn" if _romanized_hindi(message) else "en"


def language_directive(lang: str) -> str:
    """Reply-language instruction for the LLM.

    Always derived from the LATEST message, never from the conversation:
    a Telugu question followed by an English one gets an English answer,
    even though the recent-turns context is full of Telugu. That is why
    "en" returns an explicit instruction instead of nothing — without it
    the model happily continues in whatever language the history is in.
    """
    if lang == "en":
        return (
            "Reply in English — the language of the user's LATEST message. "
            "Even if earlier turns of the conversation are in another "
            "language, answer this message in English (unless the user "
            "explicitly asks you to switch or translate)."
        )
    name = LANGUAGE_NAMES.get(lang, lang)
    script_note = (
        " Write your reply in Latin script too, the way the user typed."
        if lang.endswith("-Latn")
        else ""
    )
    return (
        f"Reply in {name} — the language of the user's LATEST message, "
        f"regardless of the language of earlier turns.{script_note} "
        "Keep medical terms clear; you may give key terms in both that "
        "language and English. You can translate between the two: if the "
        "user asks for English, asks you to translate, or pastes text to "
        "translate, provide a faithful translation instead."
    )

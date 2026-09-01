"""Deterministic red-flag triage.

DRAFT — pending clinician sign-off. This curated phrase table is a SEVERITY
FLOOR that runs before any keyword gate, handler, or LLM. The LLM is never the
arbiter of emergencies. Matching is case-insensitive substring over the curated
lists; the result is the MAXIMUM tier matched, and nothing here ever downgrades
a level set elsewhere.

This same vocabulary is reused by conversation compaction (one vocabulary).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.triage.red_flags_i18n import (
    I18N_EMERGENCY_PHRASES,
    I18N_HIGH_PHRASES,
    I18N_SELF_HARM_PHRASES,
)

# Ordered severity levels. "none" < "high" < "emergency".
NONE = "none"
HIGH = "high"
EMERGENCY = "emergency"
LEVEL_ORDER = {NONE: 0, HIGH: 1, EMERGENCY: 2}


def max_level(a: str, b: str) -> str:
    """Return the higher of two levels (a floor never downgrades)."""
    return a if LEVEL_ORDER[a] >= LEVEL_ORDER[b] else b


# --- Curated phrase tables (DRAFT) ------------------------------------------
EMERGENCY_PHRASES: tuple[str, ...] = (
    "unconscious",
    "unconsciousness",
    "passed out",
    "not breathing",
    "can't breathe",
    "cannot breathe",
    "stopped breathing",
    "choking",
    "gasping",
    "seizure",
    "convulsion",
    "cardiac arrest",
    "no pulse",
    "face drooping",
    "drooping face",
    "face is drooping",
    "slurred speech",
    "speech is slurred",
    "slurring words",
    "one-sided weakness",
    "one sided weakness",
    "weakness on one side",
    "weak on one side",
    # Collapse / unresponsiveness (DRAFT).
    "unresponsive",
    "won't wake up",
    "wont wake up",
    "will not wake up",
    "not waking up",
    "cannot wake",
    "can't wake",
    "collapsed and won't wake",
    "collapsed and unresponsive",
    # Thunderclap headache — SAH/stroke red flag (DRAFT).
    "worst headache of my life",
    "worst headache ever",
    "thunderclap headache",
    # Cyanosis WITH collapse/floppiness — peri-arrest (DRAFT). Bare "turning
    # blue" stays HIGH; paired with limpness it is an emergency.
    "blue and limp",
    "limp and blue",
    "turning blue and limp",
    "going blue and limp",
    # Anaphylaxis (DRAFT).
    "throat is closing",
    "throat closing up",
    "throat swelling up",
    "throat is swelling shut",
    # Overdose disclosure — needs the emergency department NOW (DRAFT). The
    # SELF_HARM tier holds intent phrasing; a disclosed ingestion is a medical
    # emergency first.
    "overdose",
    "overdosed",
    "took too many pills",
    "took too many tablets",
    "took too many sleeping pills",
    "taken too many pills",
    "took all my pills",
    "took all my tablets",
    "swallowed all my pills",
    # --- Hindi / Hinglish (DRAFT — pending clinician + native-speaker review)
    "behosh",            # unconscious
    "बेहोश",
    "saans nahi aa rahi",  # can't breathe
    "saans nahin aa rahi",
    "sans nahi aa rahi",
    "साँस नहीं आ रही",
    "सांस नहीं आ रही",
    "daura pada",        # seizure
    "mirgi ka daura",
    "दौरा पड़ा",
    "मिर्गी",
    "nabz nahi",         # no pulse
    "नब्ज़ नहीं",
    # --- Other Indian languages, native + romanized (DRAFT — see
    # red_flags_i18n.py; machine-authored, pending native-speaker + clinician
    # review). Before these, the deterministic floor existed only in English
    # and Hindi: a Telugu speaker typing "I can't breathe" in Telugu got no
    # escalation at all.
    *I18N_EMERGENCY_PHRASES,
)

HIGH_PHRASES: tuple[str, ...] = (
    "crushing chest pain",
    "severe chest pain",
    "severe shortness of breath",
    "can't catch my breath",
    "lips turning blue",
    "lips are turning blue",
    "lips are blue",
    "blue lips",
    "severe confusion",
    "blood in vomit",
    "blood in my vomit",
    "vomiting blood",
    "blood in stool",
    "blood in my stool",
    "coughing up blood",
    "coughing up a lot of blood",
    "coughing blood",
    "cough up blood",
    "coughing up phlegm with blood",
    # Cyanosis beyond the lips (DRAFT).
    "turning blue",
    "going blue",
    "skin turning blue",
    # Acute abdomen (DRAFT).
    "severe abdominal pain",
    "severe stomach pain",
    "severe belly pain",
    "sudden severe abdominal pain",
    # Meningitis co-occurrence baked into the phrase (DRAFT).
    "fever with a stiff neck",
    "fever and stiff neck",
    "stiff neck and fever",
    "stiff neck with fever",
    "neck stiffness and fever",
    # Syncope reported after the fact (DRAFT) — HIGH, not EMERGENCY: the
    # person is conscious enough to type, but a faint needs prompt review.
    "fainted",
    "blacked out",
    "blacking out",
    # Uncontrolled bleeding (DRAFT).
    "bleeding heavily",
    "bleeding a lot",
    "bleeding won't stop",
    "bleeding wont stop",
    "won't stop bleeding",
    "wont stop bleeding",
    # --- Hindi / Hinglish (DRAFT — pending clinician + native-speaker review)
    "seene mein tez dard",   # severe chest pain
    "seene me tez dard",
    "सीने में तेज़ दर्द",
    "khoon ki ulti",         # vomiting blood
    "khoon ki ultee",
    "खून की उल्टी",
    "saans lene mein bahut takleef",  # severe breathing difficulty
    "सांस लेने में बहुत तकलीफ",
    *I18N_HIGH_PHRASES,
)

# ACS co-occurrence: chest pain PLUS an associated feature escalates to
# EMERGENCY even if neither independently reaches that tier.
CHEST_PAIN_PHRASES: tuple[str, ...] = (
    "chest pain",
    "chest tightness",
    "chest pressure",
    "pressure in my chest",
    "tightness in my chest",
    "pain in my chest",
    # Hindi / Hinglish (DRAFT)
    "seene mein dard",
    "seene me dard",
    "सीने में दर्द",
)
ACS_ASSOCIATED_PHRASES: tuple[str, ...] = (
    "arm pain",
    "left arm",
    "jaw pain",
    "sweating",
    "cold sweat",
    "shortness of breath",
    "short of breath",
    "breathless",
    "nausea",
    # Hindi / Hinglish (DRAFT)
    "paseena",
    "पसीना",
    "saans phool",
    "सांस फूल",
)

# --- Pattern tier (DRAFT — pending clinician sign-off) -----------------------
# Stem families that substring rows cannot cover without a combinatorial
# explosion of variants. Patterns run over the SAME normalized text as the
# phrase tables (lowercased, apostrophes stripped — write "wont", not
# "won't"). Only the fixed LABEL enters matched_terms, never user text, so
# receipts stay PHI-free and the label set stays bounded.
#
# "heart attack" and "stroke" are deliberately NOT bare phrases: family
# history ("my father died of a heart attack") and education ("what causes a
# stroke?") are core product flows. Only having-one-NOW framings escalate.
EMERGENCY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("breathing difficulty (pattern)", re.compile(
        r"\b(?:trouble|difficulty|difficult|struggl\w*|hard|unable)\s+"
        r"(?:to\s+)?breath\w*"
        r"|\bcant\s+breathe?\b|\bcan\s?not\s+breathe?\b")),
    ("heart attack now (pattern)", re.compile(
        r"\b(?:having|has|getting|might be having)\s+a?\s*heart\s?attack\b"
        r"|\bheart\s?attack\s+right\s+now\b"
        r"|\bthink\b[^,.!?]{0,25}\bheart\s?attack\b")),
    ("stroke now (pattern)", re.compile(
        r"\b(?:having|is having|might be having)\s+a?\s*stroke\b"
        r"|\bstroke\s+right\s+now\b"
        r"|\bthink\b[^,.!?]{0,25}\bstroke\b")),
    ("collapsed (pattern)", re.compile(
        r"\bcollapsed?\b(?!\s+(?:into|onto|on\s+the\s+(?:bed|sofa|couch)|"
        r"laughing|with\s+laughter|in\s+laughter))")),
    ("unresponsive (pattern)", re.compile(
        r"\b(?:wont|will\s+not|doesnt|does\s+not|isnt|is\s+not)\s+"
        r"respond\w*|\bnot\s+responding\b")),
)

# Negated / historical chest-pain mentions must not feed the ACS PAIRING:
# "no chest pain, just sweating a lot" fired the full EMERGENCY directive.
# Applied ONLY to the combination rule — the direct tables stay negation-
# blind on purpose (recall first; a floor never guesses about "not").
_CHEST_NEGATION_RE = re.compile(
    r"\b(?:no|without|denies|denied|not having|dont have|do not have|"
    r"never had)\s+(?:any\s+)?(?:chest|seene)"
    r"|\bchest pain (?:is )?(?:gone|resolved|better|subsided)\b"
    r"|\bhad chest pain\b[^,.!?]{0,20}\b(?:years?|months?|last year|"
    r"back|ago)\b")

# Chest-pain stem pairs: "my chest hurts", "chest is paining" (standard Indian
# English), "pain in chest". A bare hit is HIGH; with an ACS associate it is
# EMERGENCY via the co-occurrence rule below.
CHEST_STEM_RE = re.compile(
    r"\bchest\b[^,.!?]{0,30}\b(?:pain\w*|hurt\w*|ach\w*|tight\w*|heav\w*|"
    r"pressure|burning|squeez\w*)"
    r"|\b(?:pain|ache|pressure|tightness|heaviness)\b[^,.!?]{0,20}\bchest\b")

# "Cutting down/back on <food>" is the single most common false friend for the
# self-harm table — a dietary idiom, not a disclosure. Stripped before the
# self-harm scan the same way negated chest pain is stripped before the ACS
# pairing (_CHEST_NEGATION_RE). Deliberately narrow: only the "cut down/back
# on|out" shape, so "I cut myself" and "I have been cutting" keep their
# meaning.
_CUTTING_IDIOM_RE = re.compile(
    r"cut(?:ting|s)?\s+(?:down|back)(?:\s+(?:on|to))?"
    r"|cut(?:ting|s)?\s+out"
)


# Self-harm / suicide risk phrases (DRAFT — pending clinician sign-off).
# Matching any of these is an EMERGENCY with a dedicated supportive directive.
SELF_HARM_PHRASES: tuple[str, ...] = (
    "hurt myself",
    "harm myself",
    "kill myself",
    "end my life",
    "suicide",
    "suicidal",
    "want to die",
    "dont want to live",
    "don't want to live",
    "no reason to live",
    "end it all",
    "end my own life",
    "take my own life",
    "taking my own life",
    "better off dead",
    "don't want to be here anymore",
    "dont want to be here anymore",
    # Method-specific phrasing a distressed person actually types (DRAFT).
    "cutting myself",
    # NOT a bare "been cutting": it matched "I have been cutting down on
    # sugar". A reader asking a diet question got the self-harm crisis reply
    # with a helpline number, and — since the episode floor landed — a 14-day
    # EMERGENCY episode pinning every later turn to seek-care. Measured, not
    # theorised. The self-harm senses are kept; the dietary idiom is not one.
    "been cutting myself",
    "have been cutting my",
    "keep cutting myself",
    "cut my wrists",
    "cut my wrist",
    "hang myself",
    "hanging myself",
    "shoot myself",
    "jump off a",
    "want to end it",
    "going to end it",
    # Hindi / Hinglish (DRAFT)
    "khudkushi",
    "atmahatya",
    "आत्महत्या",
    "खुदकुशी",
    "मरना चाहता",
    "मरना चाहती",
    "jeena nahi chahta",
    "jeena nahi chahti",
    "khud ko nuksan",              # harm myself
    "apne aap ko nuksan",
    "अपने आप को नुकसान",
    "खुद को नुकसान",
    "नुकसान पहुँचाना चाहता",
    "नुकसान पहुंचाना चाहता",
    *I18N_SELF_HARM_PHRASES,
)

# The fixed directive that leads every EMERGENCY response.
EMERGENCY_DIRECTIVE = (
    "This may be a medical emergency. Please call your local emergency number "
    "or go to the nearest emergency department right now."
)


@dataclass(frozen=True)
class TriageResult:
    level: str
    matched_terms: list[str] = field(default_factory=list)
    self_harm: bool = False

    @property
    def is_emergency(self) -> bool:
        return self.level == EMERGENCY

    @property
    def matched(self) -> bool:
        return self.level != NONE


_APOSTROPHES = str.maketrans({
    "'": "", "’": "", "ʼ": "", "‘": "",   # every apostrophe codepoint seen
    " ": " ",                          # NBSP -> space
    "​": "", "‌": "", "‍": "",  # zero-width chars
})


def _norm(s: str) -> str:
    """Lowercase, drop every apostrophe variant and zero-width character, and
    fold NBSP to space — 'can't', 'cant', 'canʼt' must all match."""
    return s.lower().translate(_APOSTROPHES)


def _find(text: str, phrases: tuple[str, ...]) -> list[str]:
    # text is already normalized; normalize each phrase for comparison but
    # return the human-readable phrase for the receipt/summary.
    return [p for p in phrases if _norm(p) in text]


def triage(message: str) -> TriageResult:
    """Return the severity floor for a message (case-insensitive, apostrophe-
    insensitive substring)."""
    text = _norm(message)
    matched: list[str] = []
    level = NONE

    # Strip the dietary idiom before the self-harm scan — see
    # _CUTTING_IDIOM_RE. Applied here only, so the phrase tables stay
    # negation-blind everywhere else (recall first).
    self_harm_hits = _find(_CUTTING_IDIOM_RE.sub(" ", text), SELF_HARM_PHRASES)
    if self_harm_hits:
        level = max_level(level, EMERGENCY)
        matched += self_harm_hits

    emergency_hits = _find(text, EMERGENCY_PHRASES)
    if emergency_hits:
        level = max_level(level, EMERGENCY)
        matched += emergency_hits

    # Pattern tier: only the fixed label enters matched_terms, never the text.
    for label, pattern in EMERGENCY_PATTERNS:
        if pattern.search(text):
            level = max_level(level, EMERGENCY)
            matched.append(label)

    high_hits = _find(text, HIGH_PHRASES)
    if high_hits:
        level = max_level(level, HIGH)
        matched += high_hits

    chest_text = _CHEST_NEGATION_RE.sub(" ", text)
    chest_hits = _find(chest_text, CHEST_PAIN_PHRASES)
    if CHEST_STEM_RE.search(chest_text):
        chest_hits.append("chest pain (pattern)")
    assoc_hits = _find(text, ACS_ASSOCIATED_PHRASES)
    if chest_hits and assoc_hits:
        level = max_level(level, EMERGENCY)
        matched += chest_hits + assoc_hits
    elif chest_hits:
        # Bare chest pain was previously NONE — it is at least HIGH: prompt
        # review, no reassurance, and downstream may still raise it.
        level = max_level(level, HIGH)
        matched += chest_hits

    # Deterministic, de-duplicated ordering for reproducible receipts/summaries.
    return TriageResult(
        level=level,
        matched_terms=sorted(set(matched)),
        self_harm=bool(self_harm_hits),
    )

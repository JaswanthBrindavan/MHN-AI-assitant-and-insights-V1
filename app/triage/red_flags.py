"""Deterministic red-flag triage.

DRAFT — pending clinician sign-off. This curated phrase table is a SEVERITY
FLOOR that runs before any keyword gate, handler, or LLM. The LLM is never the
arbiter of emergencies. Matching is case-insensitive substring over the curated
lists; the result is the MAXIMUM tier matched, and nothing here ever downgrades
a level set elsewhere.

This same vocabulary is reused by conversation compaction (one vocabulary).
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
    # --- Hindi / Hinglish (DRAFT — pending clinician + native-speaker review)
    "seene mein tez dard",   # severe chest pain
    "seene me tez dard",
    "सीने में तेज़ दर्द",
    "khoon ki ulti",         # vomiting blood
    "khoon ki ultee",
    "खून की उल्टी",
    "saans lene mein bahut takleef",  # severe breathing difficulty
    "सांस लेने में बहुत तकलीफ",
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


def _norm(s: str) -> str:
    """Lowercase and drop apostrophes so 'can't', 'cant', 'can’t' all match."""
    return s.lower().replace("'", "").replace("’", "")


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

    self_harm_hits = _find(text, SELF_HARM_PHRASES)
    if self_harm_hits:
        level = max_level(level, EMERGENCY)
        matched += self_harm_hits

    emergency_hits = _find(text, EMERGENCY_PHRASES)
    if emergency_hits:
        level = max_level(level, EMERGENCY)
        matched += emergency_hits

    high_hits = _find(text, HIGH_PHRASES)
    if high_hits:
        level = max_level(level, HIGH)
        matched += high_hits

    chest_hits = _find(text, CHEST_PAIN_PHRASES)
    assoc_hits = _find(text, ACS_ASSOCIATED_PHRASES)
    if chest_hits and assoc_hits:
        level = max_level(level, EMERGENCY)
        matched += chest_hits + assoc_hits

    # Deterministic, de-duplicated ordering for reproducible receipts/summaries.
    return TriageResult(
        level=level,
        matched_terms=sorted(set(matched)),
        self_harm=bool(self_harm_hits),
    )

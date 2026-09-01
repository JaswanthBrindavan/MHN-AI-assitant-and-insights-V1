"""Deterministic parsers for chat data-abilities (pure, stdlib-only).

Each parser inspects the message and returns a typed request or None:
  * document queries — "find my latest blood report", "when was my father's
    last test done"
  * tracker adds — "i had 3 cups of coffee today", "I smoked 2 cigs yesterday"
  * metric pulls — "what's my latest hba1c", "my last blood pressure"
  * health summaries — "health summary for the week / this month / yearly"
  * suggestion requests — "lifestyle tips for my diabetes"

No LLM, no DB — the handlers do the I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Shared vocab
# --------------------------------------------------------------------------- #
# Longer terms first so "my grandson" is never read as "my son".
RELATION_TERMS = (
    "grandfather", "grandmother", "grandpa", "grandma",
    "granddaughter", "grandson", "grandchild", "grandkid",
    "father", "mother", "dad", "mom", "mum", "papa", "amma", "appa",
    "husband", "wife", "brother", "sister", "son", "daughter",
    "uncle", "aunty", "auntie", "aunt", "cousin", "nephew", "niece",
)
_RELATION_CANON = {
    "dad": "father", "papa": "father", "appa": "father",
    "mom": "mother", "mum": "mother", "amma": "mother",
    "grandpa": "grandfather", "grandma": "grandmother",
    "grandkid": "grandchild",
    "aunty": "aunt", "auntie": "aunt",
}

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "half": 0.5,
    "couple of": 2, "a couple of": 2,
}


def _parse_quantity(raw: str) -> float | None:
    raw = raw.strip().lower()
    if raw in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[raw])
    try:
        return float(raw)
    except ValueError:
        return None


def find_relation(message: str) -> str | None:
    """Return the canonical relation named with a possessive ("my father's")."""
    low = message.lower()
    for term in RELATION_TERMS:
        if re.search(rf"\bmy {term}\b", low):
            return _RELATION_CANON.get(term, term)
    return None


# --------------------------------------------------------------------------- #
# Document queries
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DocumentQuery:
    kinds: tuple[str, ...]
    relation: str | None = None
    wants_date: bool = False
    # A connected member named directly ("show Bhargava's reports").
    owner_name: str | None = None


# Every kind term tolerates a plural — "pull my latest lab reports" is the
# NATURAL phrasing and used to fall through to the LLM, which then wrongly
# claimed it had no access to records.
_DOC_KIND_TERMS: tuple[tuple[str, str], ...] = (
    (
        r"blood reports?|lab reports?|blood tests?|lab tests?|test reports?"
        r"|reports?",
        "report",
    ),
    (r"scans?|x-?rays?|mri|ct|ultrasounds?|imaging", "scan"),
    (r"prescriptions?", "prescription"),
    (r"vaccinations?|vaccines?|immuni[sz]ations?", "vaccination"),
    (r"insurances?|\bpolic(?:y|ies)\b", "insurance"),
    (r"\bbills?\b|invoices?", "bill"),
    (r"\btests?\b|check-?ups?|checkups?", "report"),
    # Generic words meaning "whatever I have" — expands to every kind below.
    (r"\bdocuments?\b|\bdocs?\b|\bfiles?\b|\brecords?\b|\buploads?\b", "any"),
)

ALL_DOCUMENT_KINDS: tuple[str, ...] = (
    "report", "scan", "prescription", "vaccination", "insurance", "bill",
)

_DOC_INTENT_RE = re.compile(
    r"\b(?:find|show|get|pull( up)?|fetch|where is|when was|what was|see|open|"
    r"list|view|display|do i have|what .{0,20}do i have|"
    r"latest|last|recent|most recent|all)\b",
    re.IGNORECASE,
)
_DOC_DATE_RE = re.compile(r"\bwhen\b|\bdate\b|\bhow long ago\b", re.IGNORECASE)

# "<name>'s reports" — a single word before 's that is not a relation term
# or an everyday possessive; the handler matches it against connected
# members' names (consent-gated), so a wrong guess just resolves to nobody.
_POSSESSIVE_NAME_RE = re.compile(r"\b([a-z]{3,})'s\b")
_POSSESSIVE_STOP = frozenset(
    set(RELATION_TERMS)
    | {"today", "yesterday", "tomorrow", "doctor", "week", "month", "year",
       "one", "body", "who", "there", "let", "that", "child", "children",
       "family", "everyone", "anyone"}
)


def normalize_document_kinds(values: object) -> tuple[str, ...]:
    """Canonical document kinds from a TOOL CALL's free-form ``kinds`` list.

    The model sends whatever word it likes -- "labs", "lab_report", "scans".
    This reuses the SAME ``_DOC_KIND_TERMS`` vocabulary the message parser
    uses, so the tool path and the typed path can never drift apart.

    An unrecognised kind falls back to EVERY kind rather than none: over-
    answering is recoverable, and returning nothing is what made the tool
    look broken in the first place.
    """
    if not isinstance(values, (list, tuple)):
        values = [values] if values else []
    kinds: list[str] = []
    for raw in values:
        low = str(raw).lower().replace("_", " ")
        for pattern, kind in _DOC_KIND_TERMS:
            if re.search(rf"\b(?:{pattern})\b", low) and kind not in kinds:
                kinds.append(kind)
    if "any" in kinds:
        return ALL_DOCUMENT_KINDS
    return tuple(kinds) or ALL_DOCUMENT_KINDS


def parse_document_query(message: str) -> DocumentQuery | None:
    low = message.lower()
    if not _DOC_INTENT_RE.search(low):
        return None
    kinds: list[str] = []
    for pattern, kind in _DOC_KIND_TERMS:
        if re.search(rf"\b(?:{pattern})\b", low) and kind not in kinds:
            kinds.append(kind)
    if kinds == ["any"]:
        # "show my documents/files/records" — every kind qualifies.
        kinds = list(ALL_DOCUMENT_KINDS)
    else:
        kinds = [k for k in kinds if k != "any"]
    if not kinds:
        return None
    # "my report" (self), "my father's report" (relative), a connected
    # member named by name ("Bhargava's reports"), or the self-implying
    # "do I have …" phrasing.
    relation = find_relation(message)
    owner_name = None
    if relation is None:
        m = _POSSESSIVE_NAME_RE.search(low)
        if m and m.group(1) not in _POSSESSIVE_STOP:
            owner_name = m.group(1)
    if (
        relation is None
        and owner_name is None
        and not re.search(
            # "show ALL reports" / "list the reports" imply the reader's own
            # just as clearly as "my" — requiring the possessive dropped them
            # to the LLM (audit low).
            r"\bmy\b|\bour\b|\bdo i have\b|\ball\b|\bthe\b|\bevery\b",
            low,
        )
    ):
        return None
    return DocumentQuery(
        kinds=tuple(kinds),
        relation=relation,
        wants_date=bool(_DOC_DATE_RE.search(low)),
        owner_name=owner_name,
    )


# --------------------------------------------------------------------------- #
# Tracker adds
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrackerAdd:
    log_type: str
    quantity: float
    unit: str
    day_offset: int = 0  # 0 = today, 1 = yesterday
    # The DRINK the reader named ("wine", "whisky", "chai"), not the log type
    # it rolls up under. A bottle is 330 ml of beer and 750 of wine, so the
    # serving size cannot be looked up without it -- see
    # `app.coredata.service._VESSEL_ML`, which is keyed this way because
    # mhn-spring's own seed is.
    kind: str = ""


_QTY = r"(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|half)"
_TRACKER_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(
            # Statement verbs ("I had/drank …") plus command verbs
            # ("log/add/track/record 2 cups of coffee [to my tracker]").
            rf"\b(?:had|drank|drink|took|finished|log(?:ged)?|add|track|record)\s+{_QTY}\s*"
            r"(cups?|glass(?:es)?|mugs?|shots?|pegs?|bottles?|cans?|pints?"
            r"|litres?|liters?|ml)?\s*"
            r"(?:of\s+)?(coffee|tea|chai|water|beer|wine|whisky|whiskey|rum|vodka|alcohol|drinks?)\b",
            re.IGNORECASE,
        ),
        "qty_unit_kind",
        "",
    ),
    (
        re.compile(
            rf"\bsmoked\s+{_QTY}\s*(cigs?|cigarettes?|beedis?|bidis?|smokes?)?\b",
            re.IGNORECASE,
        ),
        "smoke",
        "smoking",
    ),
    (
        # "I had 5 cigarettes" / "log 2 cigs" — unlike "smoked", these verbs
        # need the explicit cigarette unit or they would swallow unrelated
        # counts ("had 2 idlis").
        re.compile(
            rf"\b(?:had|log(?:ged)?|add|track|record)\s+{_QTY}\s*"
            r"(cigs?|cigarettes?|beedis?|bidis?)\b",
            re.IGNORECASE,
        ),
        "smoke",
        "smoking",
    ),
)

_KIND_TO_LOG_TYPE = {
    "coffee": "coffee",
    "tea": "tea",
    "chai": "tea",
    "water": "water",
    "beer": "alcohol",
    "wine": "alcohol",
    "whisky": "alcohol",
    "whiskey": "alcohol",
    "rum": "alcohol",
    "vodka": "alcohol",
    "alcohol": "alcohol",
    "drink": "alcohol",
    "drinks": "alcohol",
}
# The vessel a bare "had a beer" means, when the message names no unit.
# Water and alcohol are stored in MILLILITRES (mhn-spring's LifestyleMetric),
# so an alcohol add with no vessel has no sanctioned size at all -- see
# `canonical_amount`. Naming the vessel per DRINK rather than per log type is
# what keeps "a beer" (a 330 ml bottle) from becoming "a drink" (nothing the
# platform can size), which the handler would then have to refuse.
_KIND_DEFAULT_UNIT = {
    "water": "glass",
    "beer": "bottle",
    "wine": "glass",
    "whisky": "peg", "whiskey": "peg", "rum": "peg", "vodka": "peg",
    "alcohol": "drink", "drink": "drink", "drinks": "drink",
}
_UNIT_CANON = {
    "cup": "cup", "cups": "cup", "mug": "cup", "mugs": "cup",
    "glass": "glass", "glasses": "glass",
    "shot": "shot", "shots": "shot", "peg": "peg", "pegs": "peg",
    "bottle": "bottle", "bottles": "bottle", "can": "can", "cans": "can",
    "pint": "pint", "pints": "pint",
    "litre": "litre", "litres": "litre", "liter": "litre", "liters": "litre",
    "ml": "ml",
    "cig": "cigarette", "cigs": "cigarette",
    "cigarette": "cigarette", "cigarettes": "cigarette",
    "beedi": "beedi", "beedis": "beedi", "bidi": "beedi", "bidis": "beedi",
    "smoke": "cigarette", "smokes": "cigarette",
}


def _day_offset(message: str) -> int:
    low = message.lower()
    if "day before yesterday" in low:
        return 2
    if "yesterday" in low or "last night" in low:
        return 1
    return 0


def parse_tracker_add(message: str) -> TrackerAdd | None:
    for pattern, mode, fixed_type in _TRACKER_PATTERNS:
        m = pattern.search(message)
        if not m:
            continue
        qty = _parse_quantity(m.group(1))
        if qty is None or qty <= 0:
            return None
        # Volumes are denominated differently: "500 ml of water" is one
        # bottle, not five hundred glasses — the flat 100 cap rejected every
        # ml-phrased hydration entry (audit medium).
        unit_hint = (m.group(2) or "").lower() if m.lastindex and m.lastindex >= 2 else ""
        cap = 5000 if ("ml" in unit_hint or "litre" in unit_hint
                       or "liter" in unit_hint) else 100
        if qty > cap:
            return None
        if mode == "smoke":
            unit = _UNIT_CANON.get((m.group(2) or "cigarettes").lower(), "cigarette")
            return TrackerAdd(
                log_type=fixed_type, quantity=qty, unit=unit,
                day_offset=_day_offset(message),
            )
        unit_raw = (m.group(2) or "").lower()
        kind = m.group(3).lower()
        log_type = _KIND_TO_LOG_TYPE.get(kind)
        if log_type is None:
            return None
        default_unit = _KIND_DEFAULT_UNIT.get(kind, "cup")
        return TrackerAdd(
            log_type=log_type,
            quantity=qty,
            unit=_UNIT_CANON.get(unit_raw, default_unit),
            day_offset=_day_offset(message),
            kind=kind,
        )
    return None


# --------------------------------------------------------------------------- #
# Metric pulls
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MetricQuery:
    metric: str  # key in METRIC_REGISTRY
    wants_trend: bool = False


# metric key → (source, source-args, display name, unit hint)
METRIC_REGISTRY: dict[str, dict] = {
    "blood_sugar": {
        "source": "vital", "vital_type": "blood_sugar",
        "display": "blood sugar", "unit": "mg/dL",
        # No logged vital? Extracted reports carry it too ("Glucose -
        # Fasting") — used as a fallback, never over a fresher vital.
        "param_terms": ("glucose", "blood sugar"),
        "param_exclude": ("urine",),
    },
    "blood_pressure": {
        "source": "vital", "vital_type": "blood_pressure",
        "display": "blood pressure", "unit": "mmHg",
    },
    "heart_rate": {
        "source": "vital", "vital_type": "heart_rate",
        "display": "heart rate", "unit": "bpm",
    },
    "spo2": {
        "source": "vital", "vital_type": "spo2",
        "display": "oxygen saturation (SpO2)", "unit": "%",
    },
    "weight": {"source": "body", "body_type": "weight", "display": "weight", "unit": "kg"},
    "bmi": {"source": "body", "body_type": "bmi", "display": "BMI", "unit": "kg/m²"},
    "hba1c": {
        # "a1c" as a substring covers the real-world spellings seen in
        # extracted reports: "HbA1c", "Hemoglobin A1c", "Glycosylated
        # Hemoglobin (HbA1c)".
        "source": "report_param",
        "param_terms": ("a1c", "glycated hemoglobin", "glycated haemoglobin",
                        "glycosylated hemoglobin", "glycosylated haemoglobin"),
        "display": "HbA1c", "unit": "%",
    },
    # Common lab parameters pulled from extracted reports (same report_param
    # machinery as HbA1c). Before these existed, everyday asks like "what was
    # my last TSH" fell through to the LLM instead of the data path.
    "total_cholesterol": {
        "source": "report_param",
        "param_terms": ("total cholesterol", "cholesterol"),
        # "cholesterol" alone must never pick up the HDL/LDL rows.
        "param_exclude": ("hdl", "ldl", "vldl"),
        "display": "total cholesterol", "unit": "mg/dL",
    },
    "ldl": {
        "source": "report_param",
        "param_terms": ("ldl cholesterol", "ldl"),
        "display": "LDL cholesterol", "unit": "mg/dL",
    },
    "hdl": {
        "source": "report_param",
        "param_terms": ("hdl cholesterol", "hdl"),
        "display": "HDL cholesterol", "unit": "mg/dL",
    },
    "triglycerides": {
        "source": "report_param",
        "param_terms": ("triglycerides", "triglyceride"),
        "display": "triglycerides", "unit": "mg/dL",
    },
    "tsh": {
        "source": "report_param",
        "param_terms": ("tsh", "thyroid stimulating hormone"),
        "display": "TSH", "unit": "mIU/L",
    },
    "creatinine": {
        "source": "report_param",
        "param_terms": ("creatinine", "serum creatinine"),
        "display": "creatinine", "unit": "mg/dL",
    },
    "uric_acid": {
        "source": "report_param",
        "param_terms": ("uric acid", "serum uric acid"),
        "display": "uric acid", "unit": "mg/dL",
    },
    "vitamin_d": {
        "source": "report_param",
        "param_terms": ("vitamin d", "25-oh vitamin d", "25 oh vitamin d"),
        "display": "vitamin D", "unit": "ng/mL",
    },
    "vitamin_b12": {
        "source": "report_param",
        "param_terms": ("vitamin b12", "vitamin b-12", "b12"),
        "display": "vitamin B12", "unit": "pg/mL",
    },
    "sgpt": {
        "source": "report_param",
        "param_terms": ("sgpt", "alt", "alanine aminotransferase"),
        "display": "SGPT (ALT)", "unit": "U/L",
    },
    "sgot": {
        "source": "report_param",
        "param_terms": ("sgot", "ast", "aspartate aminotransferase"),
        "display": "SGOT (AST)", "unit": "U/L",
    },
    "hemoglobin": {
        "source": "report_param",
        "param_terms": ("hemoglobin", "haemoglobin"),
        # "Hemoglobin A1c" sits FIRST in real extractions — plain hemoglobin
        # must never return the A1c percentage.
        "param_exclude": ("a1c", "glycated", "glycosylated"),
        "display": "hemoglobin", "unit": "g/dL",
    },
}

_METRIC_TERMS: tuple[tuple[str, str], ...] = (
    # Order matters: specific terms before generic ones ("glycated hemoglobin"
    # before "hemoglobin", "ldl/hdl" before "cholesterol").
    (r"hba1c|hb a1c|a1c|glycated h(?:a)?emoglobin", "hba1c"),
    (r"blood pressure|\bbp\b", "blood_pressure"),
    (r"blood sugar|sugar (?:level|reading)|glucose|fasting sugar", "blood_sugar"),
    (r"heart rate|pulse", "heart_rate"),
    (r"spo2|oxygen (?:level|saturation)", "spo2"),
    (r"\bweight\b", "weight"),
    (r"\bbmi\b", "bmi"),
    (r"\bldl\b", "ldl"),
    (r"\bhdl\b", "hdl"),
    (r"\btriglycerides?\b", "triglycerides"),
    (r"\b(?:total )?cholesterol\b", "total_cholesterol"),
    (r"\btsh\b|thyroid stimulating hormone", "tsh"),
    (r"\bcreatinine\b", "creatinine"),
    (r"\buric acid\b", "uric_acid"),
    (r"\bvitamin d3?\b|\b25.?oh vitamin d\b", "vitamin_d"),
    (r"\bvitamin b.?12\b|\bb12\b", "vitamin_b12"),
    (r"\bsgpt\b|\balt\b|alanine aminotransferase", "sgpt"),
    (r"\bsgot\b|\bast\b|aspartate aminotransferase", "sgot"),
    (r"\bh(?:a)?emoglobin\b|\bhb\b", "hemoglobin"),
)
_METRIC_INTENT_RE = re.compile(
    r"\b(?:what(?:'s| is| was| are)|latest|last|recent|current|my|show|check|"
    r"how (?:is|was))\b",
    re.IGNORECASE,
)
_TREND_RE = re.compile(r"\btrend|over time|history|chart|graph|progress\b", re.IGNORECASE)

# Asking how to CHANGE a number, not what it is. These questions name a metric
# and say "my", so every other guard here lets them through to the data
# handler, which then reports that it has no readings on file.
_METRIC_ADVICE_RE = re.compile(
    r"\b(?:lower|reduce|raise|increase|improve|manage|control|prevent|treat|"
    r"fix|maintain|bring down|get rid of)\b"
    r"|\bhow (?:can|do|should|would) i\b"
    r"|\bwhat (?:can|should) i (?:do|eat|take)\b"
    r"|\bnaturally\b|\bhome remed|\bdiet for\b|\bexercise[s]? for\b"
    r"|\btips\b|\badvice\b|\bhelp (?:me )?(?:with|to)\b",
    re.IGNORECASE,
)


def parse_metric_query(message: str) -> MetricQuery | None:
    low = message.lower()
    if not _METRIC_INTENT_RE.search(low):
        return None
    # Tracker adds ("I had 3 cups...") also contain "my"/"had" — the caller
    # checks tracker parsing first.
    for pattern, key in _METRIC_TERMS:
        if re.search(rf"(?:{pattern})", low):
            # Require a possessive/lookup framing to avoid hijacking general
            # education questions ("what is a normal blood pressure?").
            if re.search(r"\bnormal\b|\bideal\b|\bshould\b|\brange\b", low):
                return None
            # ADVICE is not a lookup. "how can I lower my blood pressure
            # naturally?" satisfies every test above — it contains "my" and
            # names a metric — and staging answered it with "I couldn't find
            # any blood pressure readings in your records yet." The reader
            # asked how to change a number, not what the number is.
            if _METRIC_ADVICE_RE.search(low):
                return None
            if not re.search(r"\bmy\b|\bmine\b|\blatest\b|\blast\b|\bcurrent\b", low):
                return None
            return MetricQuery(metric=key, wants_trend=bool(_TREND_RE.search(low)))
    return None


# --------------------------------------------------------------------------- #
# Stated-value check ("my sugar is 117", "bp is 150/95", "hba1c 6.8")
# --------------------------------------------------------------------------- #
# "my mother's bp is 150/95" is HER reading — assessing it as the READER's
# writes the wrong person's alarm into the conversation (audit medium).
_THIRD_PARTY_VALUE_RE = re.compile(
    r"\b(?:my|our)\s+(?:mother|mom|mum|father|dad|wife|husband|son|daughter|"
    r"brother|sister|grand\w+|aunty?|uncle|friend|neighbou?r|baby)\b[^.?!]{0,30}"
    r"\b(?:bp|blood pressure|sugar|glucose|spo2|hba1c|pulse|heart rate|"
    r"cholesterol|h(?:a)?emoglobin)\b"
    r"|\b(?:his|her|their)\s+(?:bp|blood pressure|sugar|glucose|spo2|hba1c|"
    r"pulse|heart rate|cholesterol|h(?:a)?emoglobin)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StatedValue:
    metric: str                    # range key, or "blood_pressure"
    value: float
    secondary: float | None = None  # diastolic, for blood pressure


# metric term → range key (order matters: specific before generic).
_VALUE_METRIC_TERMS: tuple[tuple[str, str], ...] = (
    (r"hba1c|hb a1c|\ba1c\b|glycated h(?:a)?emoglobin", "hba1c"),
    (r"blood pressure|\bbp\b", "blood_pressure"),
    (r"\bldl\b", "ldl"),
    (r"\bhdl\b", "hdl"),
    (r"total cholesterol|cholesterol", "total_cholesterol"),
    (r"h(?:a)?emoglobin|\bhb\b|\bhg\b", "hemoglobin"),
    (r"blood sugar|glucose|fasting sugar"
     # bare "sugar" only when NOT dietary ("sugar intake is 50 grams")
     r"|sugar(?!\s*(?:intake|consumption|grams?|gram\b|cubes?|spoons?|"
     r"in my (?:tea|coffee|diet)))", "blood_sugar"),
    (r"heart rate|pulse|heartbeat", "heart_rate"),
    (r"spo2|oxygen (?:level|saturation)|\bsats?\b(?!urday)", "spo2"),
    (r"\bbmi\b", "bmi"),
)
# Plausible reading bounds per metric — also reject spurious number matches.
_VALUE_BOUNDS: dict[str, tuple[float, float]] = {
    "hba1c": (3.0, 20.0),
    "blood_pressure": (60.0, 300.0),
    "ldl": (20.0, 500.0),
    "hdl": (10.0, 200.0),
    "total_cholesterol": (50.0, 700.0),
    "hemoglobin": (3.0, 25.0),
    "blood_sugar": (30.0, 900.0),
    "heart_rate": (25.0, 260.0),
    "spo2": (40.0, 100.0),
    "bmi": (8.0, 80.0),
}
_BP_VALUE_RE = re.compile(r"(\d{2,3})\s*/\s*(\d{2,3})")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_stated_value(message: str) -> StatedValue | None:
    if _THIRD_PARTY_VALUE_RE.search(message):
        return None  # a relative's reading, not the reader's
    """Parse a reading the user states about themselves ("my sugar is 117").

    Returns None when no metric+plausible-value pair is present. The value must
    sit within a short window after the metric term so unrelated numbers
    ("sugar, and I walked 5 km") are not picked up.
    """
    low = message.lower()
    for pattern, key in _VALUE_METRIC_TERMS:
        m = re.search(pattern, low)
        if not m:
            continue
        window = low[m.end(): m.end() + 24]
        lo, hi = _VALUE_BOUNDS[key]
        if key == "blood_pressure":
            bp = _BP_VALUE_RE.search(window) or _BP_VALUE_RE.search(low)
            if not bp:
                continue
            sysv, diav = float(bp.group(1)), float(bp.group(2))
            if lo <= sysv <= hi:
                return StatedValue("blood_pressure", sysv, diav)
            continue
        num = _NUM_RE.search(window)
        if not num:
            continue
        value = float(num.group())
        if lo <= value <= hi:
            return StatedValue(key, value)
    return None


# --------------------------------------------------------------------------- #
# Health summary
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SummaryQuery:
    period: str  # week | month | year


_SUMMARY_RE = re.compile(
    r"\b(?:health\s+)?summary\b|\bhow (?:did|have) i (?:do|done|been)\b|"
    r"\boverview of my health\b|\bhealth report card\b|"
    r"\bsummari[sz]e my health\b",
    re.IGNORECASE,
)
_PERIOD_RE: tuple[tuple[str, str], ...] = (
    (r"\byear(?:ly)?\b|\bannual\b|\b12 months\b", "year"),
    (r"\bmonth(?:ly)?\b|\b30 days\b", "month"),
    (r"\bweek(?:ly)?\b|\b7 days\b", "week"),
)


def parse_summary_query(message: str) -> SummaryQuery | None:
    low = message.lower()
    if not _SUMMARY_RE.search(low):
        return None
    if "summary" not in low and "health" not in low:
        return None
    for pattern, period in _PERIOD_RE:
        if re.search(pattern, low):
            return SummaryQuery(period=period)
    return SummaryQuery(period="week")


# --------------------------------------------------------------------------- #
# MCP suggestion requests
# --------------------------------------------------------------------------- #
_SUGGESTION_RE = re.compile(
    r"\bsuggestions?\b|\bsuggest\b|\btips\b|\badvice\b|"
    r"\bwhat (?:should|can) i do\b|"
    r"\bhow (?:do|can) i (?:manage|prevent|improve|control)\b|"
    r"\blifestyle changes?\b|\bdiet plan\b|\bprecautions?\b",
    re.IGNORECASE,
)


def parse_suggestion_query(message: str) -> bool:
    return bool(_SUGGESTION_RE.search(message))


# --------------------------------------------------------------------------- #
# App-data lookups: family connections & doctor consults
# --------------------------------------------------------------------------- #
# "my family history" belongs to the insights data path, so these patterns
# anchor on connect/members/who-phrasing and never on "family" alone.
_FAMILY_LIST_RE = re.compile(
    r"\bwho(?:\s+all)?(?:\s+(?:is|are))?(?:\s+there)?\s+in\s+my\s+family\b"
    r"(?!\s+histor)"
    r"|\bmy\s+family\s+connect(?:ion)?s?\b"
    r"|\blist\s+my\s+family\b"
    r"|\bmy\s+family\s+members\b"
    r"|\bwho\s+am\s+i\s+connected\s+(?:to|with)\b",
    re.IGNORECASE,
)

_DOCTOR_CONSULT_RE = re.compile(
    r"\b(?:whom|who|which\s+doctor)\s+did\s+i\s+(?:last\s+)?"
    r"(?:consult|see|visit)\b"
    r"|\bmy\s+(?:last|latest|recent)\s+(?:doctor\s+)?consult(?:ation)?s?\b"
    r"|\bmy\s+doctor\s+connect(?:ion)?s?\b"
    r"|\bwho\s+(?:is|are)\s+my\s+doctors?\b"
    r"|\bwhich\s+doctors?\s+am\s+i\s+connected\s+(?:to|with)\b",
    re.IGNORECASE,
)


def parse_family_list_query(message: str) -> bool:
    return bool(_FAMILY_LIST_RE.search(message))


def parse_doctor_consult_query(message: str) -> bool:
    return bool(_DOCTOR_CONSULT_RE.search(message))


# --------------------------------------------------------------------------- #
# Fuzzy fallback for document queries ("pull my latest lab reprots")
# --------------------------------------------------------------------------- #
# Deterministic stdlib spelling tolerance: when the STRICT parse fails, each
# unknown word (>=4 chars) is corrected to the closest document-vocabulary
# word at a conservative similarity cutoff, and the parse is retried once.
# Correction only ever targets this closed vocabulary, so everyday words
# cannot be pulled into new meanings elsewhere in the pipeline.
_DOC_VOCAB: tuple[str, ...] = (
    "report", "reports", "blood", "lab", "test", "tests", "scan", "scans",
    "xray", "xrays", "x-ray", "x-rays", "mri", "ultrasound", "imaging",
    "prescription", "prescriptions", "vaccination", "vaccinations",
    "vaccine", "vaccines", "immunization", "immunisation", "checkup",
    "checkups", "latest", "last", "recent", "find", "show", "fetch", "pull",
    "list", "view", "display", "documents", "files", "records",
)
_DOC_VOCAB_SET = frozenset(_DOC_VOCAB)


def _fuzzy_correct_doc_words(message: str) -> str | None:
    """The message with near-miss words snapped to the document vocabulary.

    None when nothing changed. Words already in the vocabulary, short words,
    and words with no close-enough match are left untouched.
    """
    import difflib

    changed = False
    out: list[str] = []
    for token in re.split(r"(\W+)", message):
        low = token.lower()
        if (
            token.isalpha()
            and len(token) >= 4
            and low not in _DOC_VOCAB_SET
        ):
            close = difflib.get_close_matches(low, _DOC_VOCAB, n=1, cutoff=0.8)
            if close and close[0] != low:
                out.append(close[0])
                changed = True
                continue
        out.append(token)
    return "".join(out) if changed else None


def parse_document_query_fuzzy(message: str) -> DocumentQuery | None:
    """Strict parse first; on failure, one retry over the spelling-corrected
    message. Deterministic (difflib), stdlib-only."""
    query = parse_document_query(message)
    if query is not None:
        return query
    corrected = _fuzzy_correct_doc_words(message)
    if corrected is None:
        return None
    return parse_document_query(corrected)


# --------------------------------------------------------------------------- #
# Document AI-result requests ("get insights for this report")
# --------------------------------------------------------------------------- #
# Answered from mhn-ai's ai-result endpoint (insights for reports, section
# extraction for other document types) — never by the chat LLM guessing.
_AI_RESULT_RE = re.compile(
    r"\b(?:get|pull|show|give(?:\s+me)?|fetch)?\s*insights?\s+"
    r"(?:for|from|on|of|about)\s+(?:this|that|my|the)\b"
    r"[^.?!]{0,30}?\b(?:reports?|results?|scans?|files?|documents?|uploads?|pdf)?\b"
    r"|\b(?:analy[sz]e|interpret)\s+(?:this|that|my|the)\s+"
    r"(?:latest\s+|last\s+|recent\s+|uploaded\s+)?(?:lab\s+)?"
    r"(?:reports?|results?|scans?|files?|documents?|uploads?|pdf)\b"
    r"|\bextractions?\s+(?:for|from|of)\s+(?:this|that|my|the)\b"
    r"|\bwhat\s+(?:do|does)\s+(?:this|that|my|the)\s+"
    r"(?:latest\s+|recent\s+)?(?:reports?|results?|scans?)\s+(?:say|show|mean)\b",
    re.IGNORECASE,
)


def parse_ai_result_query(message: str) -> bool:
    return bool(_AI_RESULT_RE.search(message))


# --------------------------------------------------------------------------- #
# Dynamic report-parameter asks ("what is my basophils")
# --------------------------------------------------------------------------- #
# The curated METRIC_REGISTRY covers the headline metrics; everything ELSE a
# lab report can carry (basophils, RDW, GGT, …) is answered dynamically: the
# asked-for term is matched against the test names actually present in the
# user's extracted reports. The handler only answers when a matching test
# exists, so ordinary questions fall through untouched.
_PARAM_ASK_RE = re.compile(
    r"(?:\b(?:what(?:'s| is| was| are)?|show(?: me)?|check)\s+(?:my|the)\s+"
    r"|^(?:my\s+)?(?=latest|last|recent|current))"
    r"(?:latest\s+|last\s+|recent\s+|current\s+)?"
    r"([a-z][a-z0-9 /().%-]{2,40}?)"
    r"(?:\s+(?:level|value|count|reading|number)s?)?\s*\??$",
    re.IGNORECASE,
)


def parse_report_param_ask(message: str) -> str | None:
    """The parameter name the user asked about, or None."""
    m = _PARAM_ASK_RE.search(message.strip())
    if not m:
        return None
    term = m.group(1).strip()
    if len(term) < 3:
        return None
    return term


def param_tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens with a plural-tolerant singular form."""
    tokens = set()
    for t in re.split(r"[^a-z0-9]+", text.lower()):
        if not t:
            continue
        tokens.add(t[:-1] if len(t) > 3 and t.endswith("s") else t)
    return tokens


# --------------------------------------------------------------------------- #
# Section-detail asks ("what's my policy number", "when does my insurance
# expire", "how much was my last bill")
# --------------------------------------------------------------------------- #
# Answered from the section_extraction fields mhn-ai wrote into the document's
# envelope. A DETAIL word is required so plain "show my insurance" stays a
# document listing.
_SECTION_ASK_KINDS: tuple[tuple[str, str], ...] = (
    (r"insurances?|\bpolic(?:y|ies)\b", "insurance"),
    (r"\bbills?\b|invoices?", "bill"),
    (r"vaccinations?|vaccines?|immuni[sz]ations?", "vaccination"),
    (r"scans?|x-?rays?|mri|ultrasounds?", "scan"),
    (r"prescriptions?", "prescription"),
)
_SECTION_DETAIL_RE = re.compile(
    r"\b(?:details?|info(?:rmation)?|numbers?|amounts?|due|expir\w*|"
    r"valid\w*|dates?|doses?|provider|company|coverage|premium|"
    r"say|says|contain\w*|how much|when)\b",
    re.IGNORECASE,
)


def parse_section_detail_query(message: str) -> str | None:
    """The section kind a detail question is about, or None."""
    low = message.lower()
    if not re.search(r"\bmy\b|\bour\b|\bdo i have\b", low):
        return None
    if not _SECTION_DETAIL_RE.search(low):
        return None
    for pattern, kind in _SECTION_ASK_KINDS:
        if re.search(rf"\b(?:{pattern})\b", low):
            return kind
    return None


# --------------------------------------------------------------------------- #
# Manual tracker pulls ("how much water did I drink this week?")
#
# These were reaching the LLM. Every one of water / steps / coffee / smoking /
# sleep is a number the reader logged themselves and we already read for the
# [P] block — answering them with a model call is 4-12s and a paraphrase where
# a lookup is ~150ms and exact.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrackerQuery:
    source: str   # "lifestyle" (summed) | "manual" (latest) | "wearable" (Sahha)
    key: str
    period: str   # week | month | year


# Term → (source, key). "water" is logged BOTH ways; the lifestyle total is the
# honest answer to "how much did I drink", so it wins.
_TRACKER_TERMS: tuple[tuple[str, str, str], ...] = (
    (r"\bwater\b|\bhydration\b", "lifestyle", "water"),
    (r"\bcoffee\b|\bcaffeine\b", "lifestyle", "coffee"),
    (r"\btea\b", "lifestyle", "tea"),
    (r"\balcohol\b|\bdrink(?:s|ing)? alcohol\b|\bbeer\b|\bwine\b", "lifestyle", "alcohol"),
    (r"\bsmok(?:e|ed|ing)\b|\bcigarette", "lifestyle", "smoking"),
    # Wearable metrics fall back to manual_tracking (steps, sleep) or to the
    # logged vital (resting heart rate) when Sahha has nothing, so an account
    # with no connected device keeps the answer it had.
    (r"\bsteps?\b|\bwalk(?:ed|ing)?\b", "wearable", "steps"),
    (r"\bsleep(?:ing)?\b|\bslept\b", "wearable", "sleep_duration"),
    # NOT a bare "heart rate": this handler runs six slots ABOVE
    # handle_metric_query, so a bare term would hijack "what is my heart rate"
    # away from vital_reading.
    (r"\bresting heart rate\b|\brhr\b", "wearable", "heart_rate_resting"),
    (r"\bhrv\b|\bheart rate variability\b", "wearable",
     "heart_rate_variability_sdnn"),
    (r"\bcalorie", "manual", "calories"),
    # Medications come from medicine_tracking, not a habit log. Without this,
    # "show me my meds" fell through to the data-query router and came back
    # "I don't have any family-history insights on record for you yet."
    (
        r"\bmeds?\b|\bmedication|\bmedicines?\b|\btablets?\b|\bpills?\b|"
        r"\bwhat am i taking\b",
        "medications",
        "medications",
    ),
)

# Asking what is RECOMMENDED, not what the reader logged. Shared by every
# parser that reads personal data, so "need" cannot mean a norm to one of them
# and a lookup to another: "how many hours of sleep do i need" is answered by
# the validated corpus, never by 28 days of the reader's own nights.
_NORM_QUESTION_RE = re.compile(
    r"\bshould\b|\bnormal\b|\bideal\b|\brecommended\b|\benough\b"
    r"|\bneed(?:s|ed)?\b|\bsupposed to\b",
    re.IGNORECASE,
)

_TRACKER_LOOKUP_RE = re.compile(
    # Widened after a staging run. "am i smoking less these days" carried none
    # of the original keywords, so it fell through to the model, which then
    # said "I don't have access to any smoking logs or trackers" — a claim that
    # is simply untrue: lifestyle_totals reads exactly that. A missed parse is
    # not a neutral fallback here; it invents a limitation.
    r"\bhow (?:much|many|long|often)\b|\bdid i\b|\bhave i\b|\bam i\b|"
    r"\bi(?:'|’)?ve been\b|\bmy\b|\bshow\b|\blist\b|\btotal\b|"
    r"\baverage\b|\bthis (?:week|month|year)\b|"
    # A named calendar window is itself the framing cue. Without these,
    # "water intake yesterday" carried none of the others and did not parse at
    # all -- the bypass class: a deterministic slot reachable only if the
    # parser succeeds. It fell through to the model, which answered a question
    # about the reader's own log from its own weights.
    r"\byesterday\b|\blast week\b|\btoday\b|\blast night\b|"
    r"\b(?:lately|recently|these days|so far)\b",
    re.IGNORECASE,
)

# The ONE period vocabulary. `app/chat/tools/definitions.py` builds the tool
# enum from this and `tracker_query_for` coerces against it, so the free-text
# parser and the tool cannot come to mean different windows.
#
# week/month/year are ROLLING (now minus N days, what they have always meant --
# a bare "how much water" is still a rolling 7 days). The three calendar values
# are bounded at both ends; see `app.coredata.service.calendar_window`.
TRACKER_PERIODS = (
    "week", "month", "year", "today", "yesterday", "this_week", "last_week",
)

# Calendar cues, tried FIRST: "last week" contains "week", and "this week" is
# already a framing cue in _TRACKER_LOOKUP_RE, so a plain \bmonth\b/\byear\b
# ladder would quietly hand both of them the rolling default.
_CALENDAR_PERIOD_RE: tuple[tuple[str, str], ...] = (
    # "today" is the most common hydration question there is, and it used to
    # become the ROLLING WEEK: "how much water today" answered "14000 ml in
    # the past 7 days". Both rollups carry today's bucket; only the window
    # vocabulary was missing the word.
    (r"\btoday\b|\bthis morning\b", "today"),
    # "last night" is how a night's sleep is actually asked about, and this
    # module already treats it as YESTERDAY for writes -- `_day_offset` maps
    # it to 1 day. So "drank 2 glasses of water last night" logged to
    # yesterday while "how much water last night" read the rolling week: one
    # module, two answers, and the two engines then disagreed as well.
    #
    # The lookbehind keeps "the day before yesterday" out of this row.
    # `\byesterday\b` matched inside the longer phrase and the ladder returns
    # on the first hit, so a question about Sunday was answered with Monday's
    # number, in a sentence that said "yesterday". Falling through to the
    # rolling default is not right either, but it LABELS the window it used.
    (r"(?<!day before )\byesterday\b|\blast night\b|\bovernight\b",
     "yesterday"),
    (r"\blast week\b|\bprevious week\b", "last_week"),
    (r"\bthis week\b|\bweek so far\b|\bso far this week\b", "this_week"),
)


def _tracker_period(low: str) -> str:
    for pattern, period in _CALENDAR_PERIOD_RE:
        if re.search(pattern, low):
            return period
    if re.search(r"\bmonth\b|\b30 days\b", low):
        return "month"
    if re.search(r"\byear\b", low):
        return "year"
    return "week"


def tracker_query_for(metric: str, period: str) -> TrackerQuery | None:
    """Resolve a TOOL's structured metric argument against the same term table.

    Shares ``_TRACKER_TERMS`` with the free-text parser, so the tool and the
    legacy chain can never disagree about what "sleep" means, but skips the
    framing/norm/advice guards: a tool call is already an explicit request for
    a lookup, and synthesising an English sentence for a parser to re-read is
    how ``get_documents`` came to answer nothing on the agentic engine.
    """
    low = metric.strip().lower()
    if period not in TRACKER_PERIODS:
        period = "week"
    for pattern, source, key in _TRACKER_TERMS:
        if re.search(pattern, low):
            return TrackerQuery(source=source, key=key, period=period)
    return None


# --------------------------------------------------------------------------- #
# Two-metric questions ("does coffee affect my sleep") — a co-occurrence
# readout, NOT a total of either metric.
#
# These parsed as a plain tracker query until now: "does coffee affect my
# sleep" came back as a week's coffee total, because `parse_tracker_query`
# scans `_TRACKER_TERMS` in order and coffee is above sleep. That is why the
# handler runs in the SHARED prologue, above the tracker slot, and why this
# parser has to claim the message before that one does.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CorrelationQuery:
    input_key: str               # lifestyle log_type
    # sahha metric key, or None: the reader named an outcome that has no daily
    # device series, and the handler says so rather than staying silent.
    outcome_metric: str | None
    # Why the message is CLAIMED but not answerable as a co-occurrence.
    # "medication" -- the reader asked what a drug is doing to a reading,
    # which is a side-effect question. Claiming it and saying so is the point:
    # returning None sent it to `parse_tracker_query`, which answered
    # "does my blood pressure tablet affect my steps when i drink coffee"
    # with a coffee-vs-steps readout and never mentioned the drug.
    declined: str = ""


# The curated pair registry: five logged habits against four device readings.
# Never a scan of every possible pair, and never a MEDICATION on either side —
# a drug beside a symptom is a side-effect claim, which is medical advice.
# Both sides are read off `_TRACKER_TERMS`, so there is exactly one term table
# and "sleep" cannot come to mean different things to the two parsers.
#
# Every entry has a DAILY series on both sides: a `lifestyle_daily_total` for
# the habit, a `sahha_daily_total` for the reading. Smoking and steps both do
# and were simply missing, which left "does smoking affect my sleep" — the
# most likely question of this shape there is — answered as a cigarette count.
CORRELATION_INPUTS = ("water", "coffee", "tea", "alcohol", "smoking")
CORRELATION_OUTCOMES = (
    "sleep_duration", "steps", "heart_rate_resting",
    "heart_rate_variability_sdnn",
)

# A bare "heart rate", for THIS parser only. `_TRACKER_TERMS` deliberately
# omits it so "what is my heart rate" reaches `handle_metric_query` — but that
# left "does alcohol affect my heart rate", the most natural phrasing there
# is, unable to reach this guard at all, and the model answered a two-metric
# question about the reader's body from its own weights. Consulted only after
# the term table has failed, so "resting heart rate" and "heart rate
# variability" still match themselves first.
_BARE_HEART_RATE_RE = re.compile(r"\bheart rate\b|\bpulse\b", re.IGNORECASE)

# An outcome with no daily device series to line a habit up against. Saying
# "I cannot pair those" is the point: returning None instead hands the message
# to `parse_tracker_query`, which answered "does coffee affect my blood
# pressure" with a coffee total — the bypass this slot exists to close.
_UNPAIRABLE_OUTCOME_RE = re.compile(
    r"\bblood pressure\b|\bbp\b|\bblood sugar\b|\bglucose\b"
    r"|\bsugar levels?\b|\bweight\b|\bcholesterol\b|\bcalorie",
    re.IGNORECASE,
)

# The reader talking about THEMSELVES. Without this, "does coffee affect
# sleep" — a question about the world, which the validated corpus answers —
# was claimed here and answered out of the reader's private log, above the
# scope guard and above RAG; "what is the relationship between alcohol and
# sleep" came back as a count of the reader's own drinking days.
_FIRST_PERSON_RE = re.compile(
    r"\bmy\b|\bmine\b|\bme\b|\bi\b|\bi(?:\u2019|')(?:ve|m|d)\b",
    re.IGNORECASE,
)

# A relational cue is REQUIRED. Two metrics named without one ("how much coffee
# and how much sleep") is still a lookup, and stays on the path it has today.
_CORRELATION_CUE_RE = re.compile(
    r"\baffect\w*\b|\beffect\b|\bimpact\w*\b|\bcorrelat\w*\b|"
    r"\brelated\b|\brelationship\b|\blink(?:ed)?\b|\bconnection\b|"
    r"\bbecause of\b|\bdue to\b|\bwhen i\b|\bwhen i(?:'|’)?ve\b|"
    r"\bon (?:the )?days\b|\bdays (?:i|when)\b|\bafter i\b|"
    r"\bcompared? (?:to|with)\b|\bvs\.?\b|\bversus\b|"
    # The CAUSAL half, which the list was built around and then omitted:
    # "does coffee cause my poor sleep" and "does coffee make me sleep less"
    # are the two most causal phrasings there are, and both fell through --
    # the first to a coffee total, the second to the model.
    r"\bcaus\w*\b|\bmakes?\s+me\b|\bmaking\s+me\b|\bmade\s+me\b|"
    r"\blead(?:s|ing)?\s+to\b|\btrigger\w*\b|\bworsen\w*\b|\bruin\w*\b|"
    r"\bkeep(?:s|ing)?\s+me\b|\bstop(?:s|ping)?\s+me\b|"
    # The ATTRIBUTION half. "is my coffee the reason I sleep badly" and
    # "why is my sleep so bad, is it the coffee" name a cause without using
    # a causal verb, so the slot never claimed them -- and the agentic engine
    # then answered from its own weights, stating causation about the
    # reader's own records AND recommending they cut coffee out. That is the
    # single worst output this module exists to prevent.
    r"\bthe reason\b|\bresponsible for\b|\bto blame\b|\bblame\w*\b|"
    r"\bis it (?:the|my|because)\b|\bdown to\b|\bthanks to\b|"
    r"\b(?:lower|higher|less|more|shorter|longer|worse|better|bad|good)\s+"
    r"(?:when|on|after|if)\b",
    re.IGNORECASE,
)
# "is my hrv lower when i drink" — bare "drink", no beverage named, means
# alcohol. Only consulted when no habit term matched at all, and only when the
# message names no other drinkable thing.
# ponytail: naive heuristic; the sentence says "you logged alcohol" so a wrong
# guess is visible to the reader. Ask which they meant if that stops being enough.
_BARE_DRINK_RE = re.compile(r"\bdrink(?:s|ing)?\b", re.IGNORECASE)
_OTHER_DRINK_RE = re.compile(
    r"\b(?:energy|soft|fizzy|juice|milk|soda|cola|smoothie)\b", re.IGNORECASE
)


def parse_correlation_query(message: str) -> CorrelationQuery | None:
    """One logged habit and one device reading, asked about together. Else None.

    Deliberately does NOT apply `parse_tracker_query`'s ADVICE guard: "is my
    hrv lower when i drink" contains "lower", which that guard rejects, and it
    is exactly the phrasing this exists to answer. It DOES apply the norm
    guard, which was load-bearing and got dropped alongside it: "how many
    hours of sleep do i need when i drink coffee" asks what a person needs,
    and 28 days of the reader's own nights does not address that at all.

    Two gates beyond a curated pair and a relational cue:

    * a FIRST-PERSON marker, so a question about the world stays one and
      reaches the corpus instead of the reader's private log;
    * an unpairable-outcome branch, which returns a query with NO outcome
      rather than None — returning None hands the message to
      `parse_tracker_query`, and that is what made "does coffee affect my
      blood pressure" come back as a coffee total.

    A MEDICATION on either side is declined by name, above every habit term.
    The check used to sit inside the bare-"drink" branch, where it guarded
    nothing once any habit matched: "does my blood pressure tablet affect my
    steps when i drink coffee" parsed as coffee-vs-steps and the drug was
    never mentioned in the answer. Note the limit — this is a pure parser, so
    it sees medication NOUNS, not a bare brand or generic name.
    """
    low = message.lower()
    if not _CORRELATION_CUE_RE.search(low):
        return None
    if not _FIRST_PERSON_RE.search(low):
        return None
    if _NORM_QUESTION_RE.search(low):
        return None
    if _MED_CONTEXT_RE.search(low):
        return CorrelationQuery(
            input_key="", outcome_metric=None, declined="medication"
        )
    input_key: str | None = None
    outcome: str | None = None
    for pattern, source, key in _TRACKER_TERMS:
        if not re.search(pattern, low):
            continue
        if input_key is None and source == "lifestyle" and key in CORRELATION_INPUTS:
            input_key = key
        elif outcome is None and source == "wearable" and key in CORRELATION_OUTCOMES:
            outcome = key
    if (
        input_key is None
        and _BARE_DRINK_RE.search(low)
        and not _OTHER_DRINK_RE.search(low)
        # (The medication check that used to live here is now at the top of
        # the function, where it covers every branch rather than this one.)
    ):
        input_key = "alcohol"
    if input_key is None:
        return None
    if outcome is None and _BARE_HEART_RATE_RE.search(low):
        outcome = "heart_rate_resting"
    if outcome is None:
        if _UNPAIRABLE_OUTCOME_RE.search(low):
            return CorrelationQuery(input_key=input_key, outcome_metric=None)
        return None
    return CorrelationQuery(input_key=input_key, outcome_metric=outcome)


def parse_tracker_query(message: str) -> TrackerQuery | None:
    """A lookup of the reader's OWN logged tracker data. None if not one.

    Checked AFTER tracker_add by the caller: "log 2 glasses of water" and "how
    much water did I drink" share every noun, and only the framing differs.
    """
    low = message.lower()
    # A two-metric question is not a request for a total of either one.
    # "does coffee affect my sleep" satisfies every gate below and came back a
    # coffee total; declining here is what lets the co-occurrence handler above
    # the tracker slot ever see the phrasings it exists for.
    if parse_correlation_query(message) is not None:
        return None
    if not _TRACKER_LOOKUP_RE.search(low):
        return None
    # A question about the NORM is not a question about the reader's log.
    # "how much water should I drink" satisfies the lookup framing ("how much")
    # and names a tracked habit, but it is asking what is recommended. The
    # metric parser has carried this guard for a while; the suite caught this
    # parser going without it.
    if _NORM_QUESTION_RE.search(low):
        return None
    # Advice about a habit is not a reading of it either.
    if _METRIC_ADVICE_RE.search(low):
        return None
    for pattern, source, key in _TRACKER_TERMS:
        if re.search(pattern, low):
            return TrackerQuery(source=source, key=key, period=_tracker_period(low))
    return None


# --------------------------------------------------------------------------- #
# Medication commands ("add metformin 500mg", "stopped my amoxicillin",
# "remove atorvastatin") — the write goes to mhn-spring, never Davi's DB.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MedicationCommand:
    action: str  # "add" | "stop" | "remove"
    name: str
    strength: str | None = None  # "500 mg", "10mg"
    is_prn: bool = False


# Verb → action. Stop and complete/finish are the same thing to Spring (the
# course ends); remove/delete erases it. "add/start/began taking" opens one.
_MED_ADD_RE = re.compile(
    r"\b(?:add|start(?:ed)?|began|begin|log|record|put me on|now (?:on|taking)|"
    r"prescribed)\b",
    re.IGNORECASE,
)
_MED_STOP_RE = re.compile(
    r"\b(?:stop(?:ped)?|complet(?:e|ed)|finish(?:ed)?|done with|"
    r"came off|got off|no longer (?:on|taking)|ended)\b",
    re.IGNORECASE,
)
_MED_REMOVE_RE = re.compile(
    r"\b(?:remove|delete|take (?:it |this )?off (?:my )?list|"
    r"get rid of|clear)\b",
    re.IGNORECASE,
)
# A medication signal must be present so "stop worrying" / "add sugar" never
# parse as med commands: either a context word, or a real dose unit (mg/mcg/
# ml/iu — NOT bare "units", which alcohol uses). The parser stays conservative
# on purpose; the agentic engine's model handles the fuzzier phrasings
# ("I stopped my amoxicillin") the deterministic path deliberately skips.
_MED_CONTEXT_RE = re.compile(
    r"\b(?:medication|medicine|med|meds|tablet|tablets|pill|pills|capsule|"
    r"capsules|drug|dose|course|syrup|injection)\b",
    re.IGNORECASE,
)
_DOSE_UNIT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|ml|iu)\b", re.IGNORECASE
)
# "metformin 500 mg", "10mg atorvastatin" — strength is optional.
_STRENGTH_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s?(mg|mcg|g|ml|iu|units?)\b", re.IGNORECASE
)
# The drug name: a word (optionally hyphenated) after the verb, before a
# strength/frequency clause. Deliberately conservative — a real medicine name
# is a single token here; multi-word brands are matched by the DB resolve step
# when stopping/removing.
_MED_NAME_RE = re.compile(
    r"\b(?:my |the |this )?([a-z][a-z0-9\-]{2,40})\b", re.IGNORECASE
)
_MED_STOPWORDS = frozenset({
    "add", "start", "started", "began", "begin", "log", "record", "stop",
    "stopped", "complete", "completed", "finish", "finished", "remove",
    "delete", "taking", "take", "took", "put", "now", "prescribed", "done",
    "came", "got", "off", "longer", "ended", "get", "rid", "clear", "medication",
    "medicine", "med", "meds", "tablet", "tablets", "pill", "pills", "capsule",
    "capsules", "drug", "dose", "course", "syrup", "injection", "the", "this",
    "that", "my", "for", "and", "with", "daily", "twice", "once", "every",
    "morning", "night", "evening", "needed", "prn",
})


def _med_name(message: str, strength_match: re.Match[str] | None) -> str | None:
    """First plausible drug-name token that is not a command/stop word."""
    # Search the whole message; the strength (if any) marks a natural end but a
    # name can also follow it ("500mg of metformin").
    for m in _MED_NAME_RE.finditer(message):
        token = m.group(1)
        low = token.lower()
        if low in _MED_STOPWORDS or low.isdigit():
            continue
        # Skip a pure unit token that the strength regex owns.
        if re.fullmatch(r"mg|mcg|ml|iu|units?|g", low):
            continue
        return token
    return None


def parse_medication_command(message: str) -> MedicationCommand | None:
    """Parse an add/stop/remove medication instruction, or None.

    Requires BOTH an action verb AND a medication-context word, so ordinary
    talk ("stop worrying", "tell me about metformin") never matches. "tell me
    about X" has no action verb; "I take metformin" (bare statement) is left to
    the model — only an explicit add/stop/remove is a command.
    """
    if not (_MED_CONTEXT_RE.search(message) or _DOSE_UNIT_RE.search(message)):
        return None
    if _MED_REMOVE_RE.search(message):
        action = "remove"
    elif _MED_STOP_RE.search(message):
        action = "stop"
    elif _MED_ADD_RE.search(message):
        action = "add"
    else:
        return None
    strength_match = _STRENGTH_RE.search(message)
    name = _med_name(message, strength_match)
    if name is None:
        return None
    strength = None
    if strength_match:
        strength = f"{strength_match.group(1)} {strength_match.group(2).lower()}"
    is_prn = bool(re.search(r"\bas needed\b|\bprn\b|\bwhen (?:needed|required)\b",
                            message, re.IGNORECASE))
    return MedicationCommand(
        action=action, name=name, strength=strength, is_prn=is_prn
    )

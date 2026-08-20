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
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Shared vocab
# --------------------------------------------------------------------------- #
RELATION_TERMS = (
    "father", "mother", "dad", "mom", "mum", "papa", "amma", "appa",
    "husband", "wife", "brother", "sister", "son", "daughter",
    "grandfather", "grandmother", "grandpa", "grandma",
)
_RELATION_CANON = {
    "dad": "father", "papa": "father", "appa": "father",
    "mom": "mother", "mum": "mother", "amma": "mother",
    "grandpa": "grandfather", "grandma": "grandmother",
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
    (r"\btests?\b|check-?ups?|checkups?", "report"),
)

_DOC_INTENT_RE = re.compile(
    r"\b(?:find|show|get|pull( up)?|fetch|where is|when was|what was|see|open|"
    r"do i have|latest|last|recent|most recent)\b",
    re.IGNORECASE,
)
_DOC_DATE_RE = re.compile(r"\bwhen\b|\bdate\b|\bhow long ago\b", re.IGNORECASE)


def parse_document_query(message: str) -> DocumentQuery | None:
    low = message.lower()
    if not _DOC_INTENT_RE.search(low):
        return None
    kinds: list[str] = []
    for pattern, kind in _DOC_KIND_TERMS:
        if re.search(rf"\b(?:{pattern})\b", low) and kind not in kinds:
            kinds.append(kind)
    if not kinds:
        return None
    # "my report" (self) or "my father's report" (relative).
    relation = find_relation(message)
    if relation is None and not re.search(r"\bmy\b|\bour\b", low):
        return None
    return DocumentQuery(
        kinds=tuple(kinds),
        relation=relation,
        wants_date=bool(_DOC_DATE_RE.search(low)),
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


_QTY = r"(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|half)"
_TRACKER_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(
            # Statement verbs ("I had/drank …") plus command verbs
            # ("log/add/track/record 2 cups of coffee [to my tracker]").
            rf"\b(?:had|drank|drink|took|finished|log(?:ged)?|add|track|record)\s+{_QTY}\s*"
            r"(cups?|glass(?:es)?|mugs?|shots?|pegs?|bottles?|cans?|litres?|liters?|ml)?\s*"
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
_UNIT_CANON = {
    "cup": "cup", "cups": "cup", "mug": "cup", "mugs": "cup",
    "glass": "glass", "glasses": "glass",
    "shot": "shot", "shots": "shot", "peg": "peg", "pegs": "peg",
    "bottle": "bottle", "bottles": "bottle", "can": "can", "cans": "can",
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
        if qty is None or qty <= 0 or qty > 100:
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
        default_unit = {"water": "glass", "alcohol": "drink"}.get(log_type, "cup")
        return TrackerAdd(
            log_type=log_type,
            quantity=qty,
            unit=_UNIT_CANON.get(unit_raw, default_unit),
            day_offset=_day_offset(message),
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
        "source": "report_param", "param_terms": ("hba1c", "glycated hemoglobin",
                                                  "glycated haemoglobin"),
        "display": "HbA1c", "unit": "%",
    },
    # Common lab parameters pulled from extracted reports (same report_param
    # machinery as HbA1c). Before these existed, everyday asks like "what was
    # my last TSH" fell through to the LLM instead of the data path.
    "total_cholesterol": {
        "source": "report_param",
        "param_terms": ("total cholesterol", "cholesterol"),
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
            if not re.search(r"\bmy\b|\bmine\b|\blatest\b|\blast\b|\bcurrent\b", low):
                return None
            return MetricQuery(metric=key, wants_trend=bool(_TREND_RE.search(low)))
    return None


# --------------------------------------------------------------------------- #
# Stated-value check ("my sugar is 117", "bp is 150/95", "hba1c 6.8")
# --------------------------------------------------------------------------- #
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
    (r"blood sugar|sugar|glucose|fasting sugar", "blood_sugar"),
    (r"heart rate|pulse|heartbeat", "heart_rate"),
    (r"spo2|oxygen (?:level|saturation)|sat", "spo2"),
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
@dataclass(frozen=True)
class SuggestionQuery:
    # Conditions are resolved by the caller via the registry index.
    explicit: bool = True
    raw_message: str = field(default="")


_SUGGESTION_RE = re.compile(
    r"\bsuggestions?\b|\bsuggest\b|\btips\b|\badvice\b|"
    r"\bwhat (?:should|can) i do\b|"
    r"\bhow (?:do|can) i (?:manage|prevent|improve|control)\b|"
    r"\blifestyle changes?\b|\bdiet plan\b|\bprecautions?\b",
    re.IGNORECASE,
)


def parse_suggestion_query(message: str) -> SuggestionQuery | None:
    if _SUGGESTION_RE.search(message):
        return SuggestionQuery(raw_message=message)
    return None


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

"""Build patient-context ([P]) text and condition scope from stored data.

Reads only — never computes insights. Serves what recompute already persisted.
For personal-symptom questions the [P] block is enriched with a compact,
factual health snapshot (recent lifestyle, latest vitals, active medications)
so the answer can be *correlated* with the reader's own recorded data — as
things to discuss with a clinician, never as a diagnosis or a stated cause.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.erasure import is_pending
from app.coredata.service import (
    BODY_METRIC_ORDER,
    MANUAL_METRIC_ORDER,
    active_medications,
    latest_body_metrics,
    latest_manual_metrics,
    latest_vital,
    lifestyle_totals,
    recent_lab_values,
    window_start,
)
from app.models.core import PedigreeCondition
from app.models.rules import InsightArtifact


def _memo_key(db: AsyncSession, user_id: uuid.UUID) -> tuple[int, uuid.UUID]:
    return (id(db), user_id)


# Per-session memo. build_patient_context is called up to twice per chat turn
# (once for the [P] block, once for suggestion scoping) and its inputs change
# only on a pedigree write, so recomputing it is two wasted queries a turn.
# Keyed on the SESSION object identity so it cannot leak across requests, and
# cleared explicitly by recompute_insights' callers.
_context_memo: dict[tuple[int, uuid.UUID], tuple[str, set[str]]] = {}


def clear_patient_context_memo(db: AsyncSession | None = None) -> None:
    """Drop memoised context. Called after a pedigree write."""
    if db is None:
        _context_memo.clear()
        return
    for key in [k for k in _context_memo if k[0] == id(db)]:
        del _context_memo[key]


async def build_patient_context(
    db: AsyncSession, user_id: uuid.UUID
) -> tuple[str, set[str]]:
    """Return (patient_context_text, condition_codes) for a user.

    The text is a short, de-identified summary of family-history conditions and
    active insight tiers, suitable for the [P] block. Condition codes are used
    to scope retrieval.

    Memoised per session — see ``_context_memo``.

    **Returns nothing while an erasure is pending.** `pedigree_conditions` and
    `insight_artifacts` are two of the eleven tables the erasure destroys, and
    the reader has been told — in the API response, not just in a docstring —
    "Davi has stopped using your information already". Gating only
    `memory_assembly` left this path open, so the turn after a "forget me"
    still carried the reader's family history, the most sensitive category
    here, into the model's prompt.

    The suppression is memoised like any other result: within one session
    (`id(db)`) the pending state cannot change, because a forget-me request and
    a chat turn are separate HTTP requests with separate sessions. Belt and
    braces, `request_erasure` clears this memo, so even a caller that did both
    on one session cannot serve a stale pre-request value.
    """
    key = _memo_key(db, user_id)
    cached = _context_memo.get(key)
    if cached is not None:
        return cached[0], set(cached[1])

    if await is_pending(db, user_id):
        _context_memo[key] = ("", set())
        return "", set()

    conditions = (
        await db.execute(
            select(PedigreeCondition).where(
                PedigreeCondition.user_id == user_id,
                PedigreeCondition.soft_deleted.is_(False),
            )
        )
    ).scalars().all()
    insights = (
        await db.execute(
            select(InsightArtifact).where(
                InsightArtifact.user_id == user_id,
                InsightArtifact.status == "active",
            )
        )
    ).scalars().all()

    codes: set[str] = {c.condition_code for c in conditions}
    codes |= {a.condition_code for a in insights}

    if not conditions and not insights:
        _context_memo[_memo_key(db, user_id)] = ("", set(codes))
        return "", codes

    displays = sorted({c.condition_display for c in conditions})
    lines: list[str] = []
    if displays:
        lines.append("Family history on record includes: " + ", ".join(displays) + ".")
    if insights:
        tiers = sorted({f"{a.condition_code} ({a.tier})" for a in insights})
        lines.append("Active family-history insights: " + ", ".join(tiers) + ".")
    result = (" ".join(lines), codes)
    _context_memo[_memo_key(db, user_id)] = (result[0], set(codes))
    return result


# --------------------------------------------------------------------------- #
# Personal-symptom detection + health snapshot
# --------------------------------------------------------------------------- #
# First-person present-experience framing → the reader is asking about their
# OWN symptom/wellbeing, so their recorded data is relevant. Educational
# framings ("what is X", "how is X diagnosed") are deliberately excluded.
#
# Widened from real user phrasings: feelings ("i feel/get/am … <state/time>"),
# first-person concern ("should I worry about my …", "is my … ok", "how is my
# …", "am I getting enough …"), possessive symptom/metric nouns ("my fatigue",
# "my sugar"), and self-referential experience ("I keep …", "I've been …").
_SYMPTOM_NOUNS = (
    "fatigue|tiredness|exhaustion|energy|headaches?|migraines?|dizziness|dizzy|"
    "nausea|pain|aches?|sleep|insomnia|weight|breathing|breath|palpitations?|"
    "heartbeat|heart rate|pulse|stress|anxiety|mood|appetite|digestion|"
    "symptoms?|vision|numbness|tingling|cramps?|swelling|"
    "blood sugar|blood pressure|\\bbp\\b|sugar|cholesterol|hba1c|bmi|vitals?|"
    "reports?|results?|readings?|levels?|medication|medicine|meds"
)
_PERSONAL_RE = re.compile(
    r"\b("
    # first-person feeling / experience
    r"i feel|i'm feeling|i am feeling|i've been feeling|i have been feeling|"
    r"i've been|i have been|i keep (?:feeling|getting)|i can'?t stop|"
    r"i'm always|i am always|i often|i sometimes|i always|"
    r"i (?:feel|get|am|feel like|wake up|struggle to) .{0,40}"
    r"(?:all the time|lately|these days|nowadays|often|every day|at night|"
    r"in the mornings?|after (?:meals?|eating|coffee|my)|before meals?|tired|"
    r"exhausted|dizzy|weak|drained|foggy|sleepy|breathless|anxious)|"
    # first-person 'why/how/should' about oneself
    r"why (?:do|am|is|are|does) (?:i|my)|how (?:is|are|am) (?:i|my)|"
    r"why can'?t i|"
    r"should i (?:be worried|worry|be concerned)|"
    r"(?:is|are) my .{0,30}(?:ok|okay|normal|fine|high|low|too|alright|"
    r"a concern|worrying|dangerous)|"
    r"am i (?:getting enough|drinking enough|sleeping enough|"
    r"eating (?:too much|enough)|at risk|okay|healthy|fine)|"
    r"is it normal (?:that i|for me)|"
    # possessive symptom / metric noun
    r"my (?:" + _SYMPTOM_NOUNS + r")"
    r")\b",
    re.IGNORECASE,
)
# Hinglish / romanized-Hindi first-person symptom framing (DRAFT).
# A symptom/state word near a "happening / why" marker counts even without an
# explicit pronoun ("din bhar neend aati rehti hai kyun").
_HINGLISH_SYMPTOM = (
    "thakan|kamzori|chakkar|dard|neend|bukhar|sust|ghabrahat|"
    "saans|jee|ulti|pet|sar dard"
)
_PERSONAL_HINGLISH_RE = re.compile(
    r"mujhe .{0,30}(?:rehti hai|rehta hai|hoti hai|hota hai|ho rahi|ho raha|"
    r"lagti hai|lagta hai|aati hai|aate hain|aata hai)|"
    r"mujhe kyun|mujhe (?:" + _HINGLISH_SYMPTOM + r")|"
    r"meri (?:tabiyat|sehat|report|sugar|bp)|"
    r"mera (?:sugar|bp|weight|vazan)|"
    # symptom word + happening/why marker, pronoun-free
    r"(?:" + _HINGLISH_SYMPTOM + r").{0,25}"
    r"(?:aati|aate|aata|rehti|rehta|hoti|hota|ho rahi|lagti|lagta|kyun)|"
    r"kyun.{0,25}(?:" + _HINGLISH_SYMPTOM + r")",
    re.IGNORECASE,
)


def is_personal_health_query(message: str) -> bool:
    """True when the reader asks about their OWN symptom/wellbeing.

    Gates the health-snapshot enrichment: general education questions should
    not be answered with the reader's private vitals in context.
    """
    return bool(_PERSONAL_RE.search(message) or _PERSONAL_HINGLISH_RE.search(message))


def _fmt_date(dt) -> str:
    try:
        return dt.strftime("%d %b %Y")
    except Exception:  # noqa: BLE001
        return ""


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


async def build_health_snapshot(db: AsyncSession, user_id: uuid.UUID) -> str:
    """A compact, factual [P]-ready summary of ALL of the reader's recorded data.

    Pulls every available personal source: recent lifestyle totals, sleep /
    activity trackers, latest vitals, body measurements, every extracted lab
    value from recent reports, and active medications. Empty string when
    nothing is on record (empty accounts stay lean). Purely descriptive — no
    thresholds, no interpretation; the model does the (cautious, correlational)
    reasoning under the prompt's rules.
    """
    lines: list[str] = []

    # 1) Lifestyle (past-week totals).
    totals = await lifestyle_totals(db, user_id, window_start("week"))
    if totals:
        order = ("coffee", "tea", "alcohol", "smoking", "water")
        parts = [f"{_num(totals[k])} {k}" for k in order if k in totals]
        if parts:
            lines.append("Lifestyle logged in the past 7 days: " + ", ".join(parts) + ".")

    # 2) Sleep / activity trackers (latest value per type, past month).
    manual = await latest_manual_metrics(db, user_id, window_start("month"))
    if manual:
        _manual_phrase = {
            "sleep": lambda v: f"{v} h of sleep",
            "steps": lambda v: f"{v} steps",
            "calories": lambda v: f"{v} kcal",
            "water": lambda v: f"{v} glasses of water",
        }
        parts = [
            _manual_phrase.get(k, lambda v, _k=k: f"{v} {_k}")(_num(manual[k].value))
            for k in MANUAL_METRIC_ORDER if k in manual
        ]
        if parts:
            lines.append("Recent activity/sleep tracking: " + ", ".join(parts) + ".")

    # 3) Latest vitals.
    vitals: list[str] = []
    bp = await latest_vital(db, user_id, "blood_pressure")
    if bp is not None:
        sec = f"/{int(bp.secondary)}" if bp.secondary is not None else ""
        vitals.append(f"blood pressure {int(bp.value)}{sec} {bp.unit or 'mmHg'}")
    sugar = await latest_vital(db, user_id, "blood_sugar")
    if sugar is not None:
        vitals.append(f"blood sugar {int(sugar.value)} {sugar.unit or 'mg/dL'}")
    hr = await latest_vital(db, user_id, "heart_rate")
    if hr is not None:
        vitals.append(f"heart rate {int(hr.value)} {hr.unit or 'bpm'}")
    spo2 = await latest_vital(db, user_id, "spo2")
    if spo2 is not None:
        vitals.append(f"SpO2 {int(spo2.value)}{spo2.unit or '%'}")
    if vitals:
        lines.append("Latest recorded vitals: " + "; ".join(vitals) + ".")

    # 4) Body measurements (all types on record).
    body = await latest_body_metrics(db, user_id)
    if body:
        parts = [
            f"{k.replace('_', ' ')} {_num(body[k].value)}{body[k].unit or ''}"
            for k in BODY_METRIC_ORDER if k in body
        ]
        if parts:
            lines.append("Body measurements: " + ", ".join(parts) + ".")

    # 5) Lab values — every extracted parameter from recent reports/scans.
    labs = await recent_lab_values(db, user_id)
    if labs:
        parts = [
            f"{lv.name} {lv.value}{(' ' + lv.unit) if lv.unit else ''}"
            for lv in labs
        ]
        lines.append("Recent lab results on record: " + "; ".join(parts) + ".")

    # 6) Active medications.
    meds = await active_medications(db, user_id)
    if meds:
        lines.append("Current medications on record: " + ", ".join(meds) + ".")

    if not lines:
        return ""
    return "The reader's own recorded data (cite as [P]):\n- " + "\n- ".join(lines)

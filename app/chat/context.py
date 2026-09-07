"""Build patient-context ([P]) text and condition scope from stored data.

Reads only — never computes insights. Serves what recompute already persisted.
For personal-symptom questions the [P] block is enriched with a compact,
factual health snapshot (recent lifestyle, latest vitals, active medications)
so the answer can be *correlated* with the reader's own recorded data — as
things to discuss with a clinician, never as a diagnosis or a stated cause.
"""

from __future__ import annotations

import logging
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
    latest_vitals,
    lifestyle_phrase,
    lifestyle_totals,
    medical_records,
    members_granting_ai_context,
    recent_lab_values,
    window_start,
)
from app.entitlements import family_context_entitled
from app.models.core import PedigreeCondition
from app.models.rules import InsightArtifact
from app.telemetry import record_fail_open

logger = logging.getLogger("davi.chat")

# Per-session memo. build_patient_context is called up to twice per chat turn
# (once for the [P] block, once for suggestion scoping) and its inputs change
# only on a pedigree write, so recomputing it is two wasted queries a turn.
#
# Stored in ``db.info`` — the session's own scratch dict — NOT a module-global
# keyed on id(db). The global version outlived its sessions: entries for dead
# sessions stayed forever (unbounded growth), and when CPython recycled a
# freed session's address for the same user, build_patient_context served the
# DEAD session's cached context without re-running the erasure gate — stale
# PHI, after the reader was told "Ink has stopped using your information".
# db.info dies with the session, so neither failure mode exists.
_MEMO_KEY = "davi_patient_context"


def _memo(db: AsyncSession) -> dict[uuid.UUID, tuple[str, set[str]]]:
    return db.info.setdefault(_MEMO_KEY, {})


def clear_patient_context_memo(db: AsyncSession | None = None) -> None:
    """Drop memoised context for this session. Called after a pedigree write
    or an erasure request. With no session (tests), nothing global exists to
    clear any more — the memo lives and dies with each session."""
    if db is not None:
        db.info.pop(_MEMO_KEY, None)


async def build_patient_context(
    db: AsyncSession, user_id: uuid.UUID
) -> tuple[str, set[str]]:
    """Return (patient_context_text, condition_codes) for a user.

    The text is a short, de-identified summary of family-history conditions and
    active insight tiers, suitable for the [P] block. Condition codes are used
    to scope retrieval.

    Memoised per session — see ``_memo`` (db.info).

    **Returns nothing while an erasure is pending.** `pedigree_conditions` and
    `insight_artifacts` are two of the eleven tables the erasure destroys, and
    the reader has been told — in the API response, not just in a docstring —
    "Ink has stopped using your information already". Gating only
    `memory_assembly` left this path open, so the turn after a "forget me"
    still carried the reader's family history, the most sensitive category
    here, into the model's prompt.

    **The Family Connect AI-context switch does not gate this — it ADDS to
    it.** That switch (`family_connect.req_ai_context_access` /
    `acc_ai_context_access`) is an OUTBOUND grant: the flag on the reader's
    side means a connected member may use the READER's data for their own
    analysis — mhn-spring's `editAccessControls` writes the caller's own side
    under "the recipient's resulting grants on our files", and the Android
    Family Permissions screen binds it under "<name> can". It says nothing
    about the reader's own chat. Their pedigree is their own record, entered
    by them the way any clinician takes a family history; what personalises
    their own answers is personalization consent, checked where the profile is
    assembled. A gate on the outbound grant here withheld a reader's own
    history from their own answers until they had shared with some relative.

    Read the other way round the switch is real, and `shared_family_history`
    (below) is what it buys: a member who granted THIS reader AI context has
    their own recorded conditions appended here, on top of everything the
    reader entered themselves. Additive, never subtractive — nothing that
    reached this block before reaches it less often now.

    The suppression is memoised like any other result: within one session
    the pending state cannot change, because a forget-me request and a chat
    turn are separate HTTP requests with separate sessions. Belt and braces,
    `request_erasure` clears this memo, so even a caller that did both on one
    session cannot serve a stale pre-request value.
    """
    memo = _memo(db)
    cached = memo.get(user_id)
    if cached is not None:
        return cached[0], set(cached[1])

    if await is_pending(db, user_id):
        memo[user_id] = ("", set())
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

    displays = sorted({c.condition_display for c in conditions})
    lines: list[str] = []
    if displays:
        lines.append("Family history on record includes: " + ", ".join(displays) + ".")
    if insights:
        tiers = sorted({f"{a.condition_code} ({a.tier})" for a in insights})
        lines.append("Active family-history insights: " + ", ".join(tiers) + ".")
    shared = await shared_family_history(db, user_id)
    if shared:
        lines.append(shared)
    result = (" ".join(lines), codes)
    memo[user_id] = (result[0], set(codes))
    return result


# --------------------------------------------------------------------------- #
# Family context — what the Family Connect AI-context switch actually buys
# --------------------------------------------------------------------------- #
_SHARED_HISTORY_LEAD = (
    "Family history from connected members' own records, shared by them for "
    "this purpose: "
)


async def shared_family_history(db: AsyncSession, user_id: uuid.UUID) -> str:
    """One [P] line: the recorded conditions of members who granted this
    reader AI context. Empty string whenever any part of the gate says no.

    THE CAPABILITY. Ink already knows the reader's family history as the
    reader typed it — a pedigree slot with a condition code. When a connected
    member has switched AI context ON for this reader, their *own* record can
    stand in for that hearsay: "your mother — Hypothyroidism" out of the
    mother's medical_condition rows, rather than only what the reader
    remembered to enter. That is the smallest thing the switch can buy that is
    genuinely analysis rather than a pull.

    TWO GATES, cheap one first:

    1. **Per-member consent**, from the shared database — the INBOUND grant
       (``members_granting_ai_context``). No granting member, no Spring call:
       most readers never pay a round trip to learn there is nothing to add.
    2. **The plan**, from mhn-spring ``GET /entitlements/{userId}``. Ink does
       not own subscriptions and does not reconstruct them.

    NOT A PULL. This never touches the member's documents, lab values or THP
    series; those are governed by the file read grant and
    ``file_access_exclusions`` and work identically with this switch off, which
    is what the owner asked for and what
    ``test_a_family_read_does_not_depend_on_the_ai_context_switch`` pins.

    ``shared_only=True`` is what keeps a PRIVATE condition out: it is
    mhn-spring's own ``getByUserIdAndIsPrivateFalse``, and a NULL (a row that
    predates the column) is not a decision to share.

    Condition CODES are deliberately not extended from this. Pedigree rows
    carry a registry code; ``medical_condition.name`` is free text, and
    mapping it would be guessing which knowledge profile to retrieve on a
    relative's behalf. The text reaches the model; retrieval scope stays the
    reader's own.
    """
    parts: list[str] = []
    try:
        # SAVEPOINT: on PostgreSQL a failed statement aborts the whole
        # transaction, so without this a broken family read would take the
        # memory read and the receipt write after it down too (audit H8).
        async with db.begin_nested():
            members = await members_granting_ai_context(db, user_id)
        if not members:
            return ""
        # Asked OUTSIDE the savepoint on purpose: this is a network call with
        # a multi-second timeout, and there is no reason to hold a savepoint
        # open across it.
        if not await family_context_entitled(user_id):
            return ""
        async with db.begin_nested():
            # ponytail: one medical_records call per granting member, bounded
            # by how many relatives both connected AND flipped the switch (a
            # handful), and paid only by readers who have one. Batch it into a
            # single IN () read if that ever stops being true — but not by
            # copying the deleted_at/private predicate, which lives in
            # medical_records precisely so it is written once.
            for member_id, relation in members:
                rows = await medical_records(
                    db, member_id, type_="condition", shared_only=True
                )
                names = sorted({r.name for r in rows if r.name})
                if not names:
                    continue
                who = f"your {relation.lower()}" if relation else (
                    "a connected family member"
                )
                parts.append(f"{who} — " + ", ".join(names))
    except Exception:  # noqa: BLE001 — enrichment must never break a reply
        logger.warning("shared family history failed; continuing", exc_info=True)
        record_fail_open("shared_family_history")
        return ""
    if not parts:
        return ""
    return _SHARED_HISTORY_LEAD + "; ".join(parts) + "."


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
        parts = [lifestyle_phrase(totals[k]) for k in order if k in totals]
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
    # ONE round trip for all four. These were four sequential queries on the
    # path that runs for every personal question.
    _v = await latest_vitals(
        db, user_id, ("blood_pressure", "blood_sugar", "heart_rate", "spo2")
    )
    bp = _v.get("blood_pressure")
    if bp is not None:
        sec = f"/{int(bp.secondary)}" if bp.secondary is not None else ""
        vitals.append(f"blood pressure {int(bp.value)}{sec} {bp.unit or 'mmHg'}")
    sugar = _v.get("blood_sugar")
    if sugar is not None:
        vitals.append(f"blood sugar {int(sugar.value)} {sugar.unit or 'mg/dL'}")
    hr = _v.get("heart_rate")
    if hr is not None:
        vitals.append(f"heart rate {int(hr.value)} {hr.unit or 'bpm'}")
    spo2 = _v.get("spo2")
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

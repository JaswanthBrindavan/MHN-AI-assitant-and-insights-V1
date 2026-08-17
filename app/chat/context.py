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

from app.coredata.service import (
    active_medications,
    latest_body_measurement,
    latest_vital,
    lifestyle_totals,
    window_start,
)
from app.models.core import PedigreeCondition
from app.models.rules import InsightArtifact


async def build_patient_context(
    db: AsyncSession, user_id: uuid.UUID
) -> tuple[str, set[str]]:
    """Return (patient_context_text, condition_codes) for a user.

    The text is a short, de-identified summary of family-history conditions and
    active insight tiers, suitable for the [P] block. Condition codes are used
    to scope retrieval.
    """
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
        return "", codes

    displays = sorted({c.condition_display for c in conditions})
    lines: list[str] = []
    if displays:
        lines.append("Family history on record includes: " + ", ".join(displays) + ".")
    if insights:
        tiers = sorted({f"{a.condition_code} ({a.tier})" for a in insights})
        lines.append("Active family-history insights: " + ", ".join(tiers) + ".")
    return " ".join(lines), codes


# --------------------------------------------------------------------------- #
# Personal-symptom detection + health snapshot
# --------------------------------------------------------------------------- #
# First-person present-experience framing → the reader is asking about their
# OWN symptom/wellbeing, so their recorded data is relevant. Educational
# framings ("what is X", "how is X diagnosed") are deliberately excluded.
_PERSONAL_RE = re.compile(
    r"\b("
    r"i feel|i'm feeling|i am feeling|i've been feeling|i have been feeling|"
    r"i've been|i have been|"
    r"why do i (?:feel|get|have|keep)|why am i|why is my|"
    r"i keep (?:feeling|getting)|i (?:feel|get|am) .{0,30}"
    r"(?:all the time|lately|these days|nowadays|often|every day)|"
    r"should i (?:be worried|worry)|is it normal (?:that i|for me)|"
    r"i can'?t stop|i'm always|i am always|"
    r"my (?:fatigue|tiredness|energy|headaches?|dizziness|dizzy|pain|sleep|"
    r"weight|symptoms?|blood sugar|blood pressure|bp|sugar)"
    r")\b",
    re.IGNORECASE,
)
# Hinglish / romanized-Hindi first-person symptom framing (DRAFT).
_PERSONAL_HINGLISH_RE = re.compile(
    r"mujhe .{0,30}(?:rehti hai|rehta hai|hoti hai|hota hai|ho rahi|ho raha|"
    r"lagti hai|lagta hai)|"
    r"mujhe kyun|mujhe (?:thakan|kamzori|chakkar|dard)",
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


async def build_health_snapshot(db: AsyncSession, user_id: uuid.UUID) -> str:
    """A compact, factual [P]-ready summary of the reader's own recorded data.

    Recent lifestyle totals, latest vitals + HbA1c + weight, and active
    medications. Empty string when nothing is on record (empty accounts stay
    lean). Purely descriptive — no thresholds, no interpretation; the model
    does the (cautious, correlational) reasoning under the prompt's rules.
    """
    from app.chat.data_handlers import _latest_report_param

    lines: list[str] = []

    totals = await lifestyle_totals(db, user_id, window_start("week"))
    if totals:
        order = ("coffee", "tea", "alcohol", "smoking", "water")
        parts = [
            f"{int(totals[k]) if float(totals[k]).is_integer() else totals[k]} {k}"
            for k in order if k in totals
        ]
        if parts:
            lines.append("Lifestyle logged in the past 7 days: " + ", ".join(parts) + ".")

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
    if vitals:
        lines.append("Latest recorded vitals: " + "; ".join(vitals) + ".")

    hba1c = await _latest_report_param(
        db, user_id, ("hba1c", "glycated hemoglobin", "glycated haemoglobin")
    )
    if hba1c is not None:
        value, unit, at = hba1c
        d = _fmt_date(at)
        lines.append(
            f"Most recent HbA1c on record: {value}{unit or '%'}"
            + (f" ({d})" if d else "") + "."
        )

    weight = await latest_body_measurement(db, user_id, "weight")
    if weight is not None:
        lines.append(f"Latest recorded weight: {weight.value:g} kg.")

    meds = await active_medications(db, user_id)
    if meds:
        lines.append("Current medications on record: " + ", ".join(meds) + ".")

    if not lines:
        return ""
    return "The reader's own recorded data (cite as [P]):\n- " + "\n- ".join(lines)

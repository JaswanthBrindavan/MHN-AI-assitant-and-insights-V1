"""Backend-sourced reference ranges (production ``thp_age_range`` data).

The clinically-curated, age-banded ideal ranges live in the production database
(``traditional_health_parameters`` → ``thp_age_range``, with graduated
min/low_danger/low_warn/ideal/high_warn/high_danger/max thresholds). This module
reads them and classifies a value into a graduated severity, so the reply can
match severity (reassure / consult a doctor / seek care promptly). When no
backend entry matches (e.g. a metric not curated, or an empty DB), the caller
falls back to the DRAFT constants in ``app.health.ranges``.

Never diagnoses — severity routes to care, it does not name a condition.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common import utcnow
from app.models.core import User
from app.models.coredata import ThpAgeRange, TraditionalHealthParameter

logger = logging.getLogger("davi.health")

# metric key → substrings to match against a THP name/alias (lowercased).
# Metric key -> the EXACT reference-parameter names that mean it.
#
# This was a substring match ("ldl" in the name), which is wrong in a
# catalogue where parameter names contain each other. Against the backend's
# real 193-row catalogue it produced three silently wrong answers:
#
#   ldl        -> "HDL/LDL Ratio"                (range 0.4-999, status DRAFT)
#                 an LDL of 190 -- statin territory -- graded NORMAL
#   hdl        -> "CHOL/HDL ratio"               (tops out at 8.4)
#                 every HDL in mg/dL graded DANGER, routed to urgent care
#   hemoglobin -> "Glycated Hemoglobin (HbA1c)"  (4-5.7 %)
#                 anaemia at 8 g/dL graded HIGH, in the wrong unit
#
# A word-boundary regex does NOT fix this: "hdl" is already a whole word in
# "CHOL/HDL ratio", and "ldl" is one in "LDL/HDL ratio" and "VLDL
# Cholesterol". Only naming the parameter works.
#
# Names verified present in the backend catalogue. An unmapped or renamed
# parameter yields NO match, which falls back to Davi's own constants in
# app/health/ranges.py -- the pre-catalogue behaviour, and clinically correct.
# Falling back is always the safe direction here.
# Keys are the metric keys app/health/ranges.py defines — keep the two in step,
# or a metric silently loses its backend range.
_THP_NAMES: dict[str, tuple[str, ...]] = {
    "blood_sugar": ("Fasting Blood Sugar", "Blood Sugar"),
    "fasting_glucose": ("Fasting Blood Sugar",),
    "random_glucose": ("Random Blood Sugar", "Post Prandial Blood Sugar"),
    "hba1c": ("Glycated Hemoglobin (HbA1c)",),
    "heart_rate": ("Pulse Rate", "Heart Rate"),
    "spo2": ("SpO2", "Oxygen Saturation"),
    "hemoglobin": ("Hemoglobin",),
    "total_cholesterol": ("Total Cholesterol",),
    "ldl": ("LDL Cholesterol",),
    "hdl": ("HDL Cholesterol",),
    "bmi": ("BMI", "Body Mass Index"),
}

# Reference data the owning team has not approved must never grade a
# patient's value. Absent columns (an older database) are treated as approved,
# because those rows predate the curation workflow.
_USABLE_STATUSES = frozenset({"approved", None})

_DEFAULT_ADULT_AGE = 40


@dataclass(frozen=True)
class BackendVerdict:
    severity: str    # "normal" | "warn" | "danger"
    direction: str   # "low" | "high" | ""
    ideal_low: float
    ideal_high: float
    unit: str
    label: str


async def user_age(db: AsyncSession, user_id: uuid.UUID) -> int | None:
    """Age in whole years from the user's DOB, or None if unknown."""
    try:
        dob = (
            await db.execute(select(User.dob).where(User.id == user_id))
        ).scalars().first()
        if dob is None:
            return None
        today = utcnow().date()
        return max(
            0,
            today.year - dob.year
            - ((today.month, today.day) < (dob.month, dob.day)),
        )
    except Exception:  # noqa: BLE001
        return None


def _classify_bands(r: ThpAgeRange, value: float) -> tuple[str, str]:
    if value <= r.low_danger:
        return "danger", "low"
    if value <= r.low_warn:
        return "warn", "low"
    if value < r.high_warn:
        return "normal", ""
    if value < r.high_danger:
        return "warn", "high"
    return "danger", "high"


async def _match_thp(
    db: AsyncSession, metric_key: str
) -> TraditionalHealthParameter | None:
    """The reference parameter for this metric, or None.

    EXACT name/alias match, not substring, and ordered so the answer is the
    same every time. None means "the backend has nothing trustworthy for this"
    and the caller falls back to Davi's own constants -- which is the safe
    direction, and was the behaviour before the catalogue was populated.
    """
    names = _THP_NAMES.get(metric_key)
    if not names:
        return None
    wanted = {n.lower() for n in names}

    rows = (
        await db.execute(
            # Deterministic: without an ORDER BY the same question could match
            # different parameters on different days, depending only on how the
            # planner returned rows.
            select(TraditionalHealthParameter).order_by(
                TraditionalHealthParameter.id
            )
        )
    ).scalars().all()

    for preferred in names:
        for thp in rows:
            if thp.status not in _USABLE_STATUSES:
                continue
            if thp.visible is False:
                continue
            candidates = {thp.name.strip().lower()}
            candidates |= {
                str(a).strip().lower() for a in (thp.aliases or []) if a
            }
            if preferred.lower() in candidates and preferred.lower() in wanted:
                return thp
    return None


async def evaluate_backend(
    db: AsyncSession, metric_key: str, value: float, age: int | None
) -> BackendVerdict | None:
    """Graduated verdict from backend ranges, or None if none match."""
    try:
        thp = await _match_thp(db, metric_key)
        if thp is None:
            return None
        a = _DEFAULT_ADULT_AGE if age is None else age
        # The age band covering the reader, else the closest by age_min.
        ranges = (
            await db.execute(
                select(ThpAgeRange)
                .where(ThpAgeRange.thp_id == thp.id)
                .order_by(ThpAgeRange.age_min)
            )
        ).scalars().all()
        if not ranges:
            return None
        chosen = next(
            (r for r in ranges if r.age_min <= a <= r.age_max), ranges[0]
        )
        severity, direction = _classify_bands(chosen, value)
        return BackendVerdict(
            severity=severity, direction=direction,
            ideal_low=chosen.low_warn, ideal_high=chosen.high_warn,
            unit=thp.units, label=thp.name,
        )
    except Exception:  # noqa: BLE001 — backend lookup must never break a reply
        logger.warning("backend range lookup failed", exc_info=True)
        return None

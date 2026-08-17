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
_THP_HINTS: dict[str, tuple[str, ...]] = {
    "blood_sugar": ("blood sugar", "glucose", "sugar"),
    "fasting_glucose": ("fasting glucose", "fasting blood sugar", "fasting sugar",
                        "fasting"),
    "random_glucose": ("random glucose", "post-meal", "postprandial", "random"),
    "hba1c": ("hba1c", "glycated"),
    "heart_rate": ("heart rate", "pulse"),
    "spo2": ("spo2", "oxygen saturation", "oxygen"),
    "hemoglobin": ("hemoglobin", "haemoglobin"),
    "total_cholesterol": ("total cholesterol", "cholesterol"),
    "ldl": ("ldl",),
    "hdl": ("hdl",),
    "bmi": ("bmi", "body mass index"),
}
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
    hints = _THP_HINTS.get(metric_key)
    if not hints:
        return None
    rows = (
        await db.execute(select(TraditionalHealthParameter))
    ).scalars().all()
    # First-hint priority: a THP whose name/alias contains an earlier hint wins.
    best: tuple[int, TraditionalHealthParameter] | None = None
    for thp in rows:
        hay = " ".join(
            [thp.name.lower(), *[str(a).lower() for a in (thp.aliases or [])]]
        )
        for rank, hint in enumerate(hints):
            if hint in hay:
                if best is None or rank < best[0]:
                    best = (rank, thp)
                break
    return best[1] if best else None


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

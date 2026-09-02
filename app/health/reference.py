"""Backend-sourced reference ranges (production ``thp_age_range`` data).

The clinically-curated, age-banded ideal ranges live in the production database
(``traditional_health_parameters`` → ``thp_age_range``, with
min/low_warn/ideal/high_warn/max thresholds). This module reads them and grades
a value against the warning band, so the reply can match it (reassure /
consult a doctor). When no backend entry matches (e.g. a metric not curated, or
an empty DB), the caller falls back to the DRAFT constants in
``app.health.ranges``.

Never diagnoses — severity routes to care, it does not name a condition.

``min``/``max`` are the graph axis bounds and grade nothing. See
``_classify_bands`` for why the old danger tier is gone and what that costs.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.health.ranges import RANGES
from app.models.common import utcnow
from app.models.core import User
from app.models.coredata import ThpAgeRange, TraditionalHealthParameter
from app.telemetry import record_fail_open

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
    severity: str    # "normal" | "warn"
    direction: str   # "low" | "high" | ""
    ideal_low: float
    ideal_high: float
    unit: str
    label: str


async def reader_bands(
    db: AsyncSession, user_id: uuid.UUID
) -> tuple[int | None, str | None]:
    """``(age in whole years, sex)`` — the two facts that pick a band.

    One query for both. `thp_age_range` is banded by age AND sex, so fetching
    them separately would cost a second round trip on a path that already has
    no budget headroom.

    Sex is the reader's own `gender` (`female` | `male` | `other`). `other`
    returns None: production seeds bands for `any`, `female` and `male` only,
    so there is nothing for it to match and it must fall back to `any` like an
    unknown does.
    """
    try:
        row = (
            await db.execute(
                select(User.dob, User.gender).where(User.id == user_id)
            )
        ).first()
        if row is None:
            return None, None
        dob, gender = row
        sex = (gender or "").strip().lower() or None
        if sex not in ("female", "male"):
            sex = None
        if dob is None:
            return None, sex
        today = utcnow().date()
        age = max(
            0,
            today.year - dob.year
            - ((today.month, today.day) < (dob.month, dob.day)),
        )
        return age, sex
    except Exception:  # noqa: BLE001
        return None, None


async def user_age(db: AsyncSession, user_id: uuid.UUID) -> int | None:
    """Age in whole years from the user's DOB, or None if unknown."""
    age, _ = await reader_bands(db, user_id)
    return age


def _classify_bands(
    r: ThpAgeRange, value: float, metric_key: str = ""
) -> tuple[str, str]:
    """(severity, direction) from ``low_warn``/``high_warn`` ONLY.

    Two zones, the model mhn-spring settled on in V28 when it dropped
    ``low_danger``/``high_danger``.

    NOTHING IS LOST BY DROPPING THE "danger" TIER, and it is not being dropped
    quietly. It read the two dropped columns, which the staff dashboard never
    collected and pinned to the graph bounds — ``low_danger == min`` and
    ``high_danger == max`` on every row V18 seeded — so it only ever fired past
    the end of the chart: LDL ≥ 228 mg/dL, total cholesterol ≥ 288, fasting
    glucose ≥ 292. An LDL of 190 or a cholesterol of 260 already graded "warn".
    The tier cannot be preserved either: the columns it read are gone, and
    re-wiring "seek care promptly" onto every out-of-range value would send an
    LDL of 105 to urgent care. So out-of-range keeps the answer it has today,
    and the answer the rest of the app gives (report flags route to
    ``discuss_with_clinician`` too — app/chat/data_handlers.py).

    No reading gets a milder reply than it gets today: since V28 this lookup
    raises before grading anything, and an extreme value gets an error page
    rather than an escalation.

    Boundaries are deliberate and pinned in tests. ``high_warn`` itself grades
    HIGH, which is what today's code does — V28's header reads the ideal band
    as inclusive at both ends, and going that way would flip an LDL of exactly
    100 from "consult your doctor" to "that's reassuring".

    ONE-SIDED METRICS. The backend gives every parameter both a `low_warn` and
    a `high_warn`, because the table has both columns — not because both ends
    are clinically meaningful. For HDL they are not: more is better, and
    production's band is male 40-60, female 50-70, so an HDL of 65 was told it
    was "above the usual range, please consult your doctor" about a GOOD
    result. Davi's own DRAFT constants already carry the direction that table
    cannot — `hdl` is `RangeSpec(40, None)`, "no upper bound is flagged" — and
    the same is true of SpO2 (higher is better) and of LDL and total
    cholesterol at the bottom end. So the curated spec, where we have one,
    decides which SIDES may warn; the backend decides where the line sits.

    A metric with no DRAFT spec keeps both sides, which is the conservative
    direction.
    """
    spec = RANGES.get(metric_key)
    if value < r.low_warn and (spec is None or spec.low is not None):
        return "warn", "low"
    if value >= r.high_warn and (spec is None or spec.high is not None):
        return "warn", "high"
    return "normal", ""


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
    db: AsyncSession,
    metric_key: str,
    value: float,
    age: int | None,
    sex: str | None = None,
) -> BackendVerdict | None:
    """Verdict from backend ranges, or None if none match.

    ``sex`` picks between the sex-specific bands production seeds for 28
    parameters. Pass it whenever it is known: without it a woman's HDL of 65
    is graded against the male band (40-60) instead of her own (50-70).

    When a parameter has ONLY sex-specific bands and the reader's sex is not
    known, this returns None rather than guessing, and the caller falls back
    to the DRAFT constants — the same safe direction every other failure here
    takes. Grading someone against the other sex's range is worse than
    grading them against a general one.
    """
    try:
        # Own SAVEPOINT, so that swallowing an error below does not poison the
        # CALLER's transaction. It did: a failed statement leaves the whole
        # transaction aborted, the ability's enclosing savepoint then failed to
        # RELEASE, the reply was discarded, and the next query on that session
        # (patient context) took the request down as a 500. Fail-open has to
        # mean the session still works, not just that this function returns.
        async with db.begin_nested():
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
            # The reader's own sex first, then the unisex band. Never the
            # other sex's: `order_by(age_min)` alone had no tiebreak, so which
            # of "HDL male 40-60" and "HDL female 50-70" a reader was graded
            # against came down to row order.
            want = (sex or "").strip().lower()
            pool = [r for r in ranges if (r.sex or "any").lower() == want]
            if not pool:
                pool = [
                    r for r in ranges if (r.sex or "any").lower() == "any"
                ]
            if not pool:
                # Sex-specific bands only, and we do not know theirs. Fall
                # back to the DRAFT constants rather than pick one.
                return None
            chosen = next(
                (r for r in pool if r.age_min <= a <= r.age_max), pool[0]
            )
            severity, direction = _classify_bands(chosen, value, metric_key)
            return BackendVerdict(
                severity=severity, direction=direction,
                ideal_low=chosen.low_warn, ideal_high=chosen.high_warn,
                unit=thp.units, label=thp.name,
            )
    except Exception:  # noqa: BLE001 — backend lookup must never break a reply
        # Still a catch-all: the caller falls back to the DRAFT constants in
        # app/health/ranges.py, which is the safe direction, and no schema
        # surprise is worth a failed reply. But LOUD — ERROR with a traceback
        # and a fail-open counter. As a WARNING this hid mhn-spring's V28 for
        # the whole life of that bug; nothing scrapes a warning nobody reads.
        logger.exception("backend range lookup failed")
        record_fail_open("backend_ranges")
        return None

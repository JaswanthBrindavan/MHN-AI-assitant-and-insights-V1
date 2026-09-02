"""Behaviour patterns: what the reader's own records did on the same days.

PURE and stdlib-only, like ``app/insights/core.py``. No DB, no LLM, no clock —
callers pass the aligned series in and get a deterministic result out, so the
same records always produce the same cards.

NAMING. ``app/insights/`` is the FAMILY-HISTORY engine and ``/api/v1/insights``
is its route. This is a different feature that the app also labels "Insights",
so it lives under its own name to stop the two being confused in code. The
screen title is presentation; the module is not.

WHAT THIS IS NOT. Not a correlation coefficient, not a p-value, not a
significance test, and not a causal claim. It reports what two of the reader's
own records did on the same days and says so in those words. The general
"caffeine is a stimulant" half comes from the clinician-reviewed corpus, which
IS validated; the personal half stays observational. Keeping those two apart is
the whole design:

    "On the 11 days you logged coffee after 3pm, your recorded sleep averaged
     38 minutes shorter than on the 14 days you did not. This might be the
     reason — though many other things differ between those days."

There is deliberately NO confidence score. A number like "confidence: high"
reads as a statistical claim we cannot support, and the owner asked for it to
be dropped. What IS reported is the plain count of days on each side, which is
the honest version of the same information.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

# Days on each side before anything is reported. Seven is a week of each
# behaviour: fewer and one unusual night sets the whole picture, which is the
# failure mode this gate exists to prevent, not a statistical threshold.
MIN_DAYS_PER_GROUP = 7

# The window a pattern is computed over. Long enough for two groups of seven,
# short enough that it describes how someone is living now.
WINDOW_DAYS = 28

# How the outcome day relates to the exposure day. These are the labels the
# design uses, and each one changes which days are compared.
LAG_SAME_DAY = "same_day"        # "same night" for sleep
LAG_NEXT_DAY = "next_day"        # "next morning" for resting HR
LAG_CUMULATIVE = "cumulative"    # a trend over the window, not day-by-day

#: Direction that counts as "better" for an outcome. Used only to word the
#: sentence, never to grade the reader.
HIGHER_IS_BETTER = frozenset({
    "sleep_duration", "steps", "heart_rate_variability_sdnn",
    "heart_rate_variability_rmssd", "spo2", "mood",
})


@dataclass(frozen=True)
class DayValue:
    """One day's figure for one outcome. ``value`` is already in its own unit."""

    day: date
    value: float


@dataclass(frozen=True)
class Observation:
    """What two of the reader's records did on the same days.

    ``enough`` False means the gate refused: ``days_with``/``days_without``
    still carry the counts, which is what the "not yet" card shows.
    """

    exposure: str
    outcome: str
    lag: str
    days_with: int
    days_without: int
    mean_with: float | None = None
    mean_without: float | None = None
    enough: bool = False
    #: Signed difference, exposure minus baseline, in the outcome's own unit.
    difference: float | None = None
    #: True when the difference points the way the reader would rather it did.
    favourable: bool | None = None
    contributing_days: tuple[date, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        """Stable id for the detail route. Same inputs, same key, always."""
        return f"{self.exposure}__{self.outcome}__{self.lag}"


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def observe(
    exposure: str,
    outcome: str,
    exposure_days: set[date],
    outcome_series: list[DayValue],
    *,
    lag: str = LAG_SAME_DAY,
    min_days: int = MIN_DAYS_PER_GROUP,
) -> Observation:
    """Compare the outcome on exposure days against every other measured day.

    ``exposure_days`` are the days the habit was LOGGED. A day with no
    lifestyle row is a day the reader did not log the thing, which is not the
    same as a day they did not do it — the wording never claims otherwise.

    Only days that appear in ``outcome_series`` are counted on either side, so
    a day the device did not sync joins neither group rather than silently
    becoming a baseline day.
    """
    if lag == LAG_NEXT_DAY:
        from datetime import timedelta

        exposure_days = {d + timedelta(days=1) for d in exposure_days}

    with_vals = [p.value for p in outcome_series if p.day in exposure_days]
    without_vals = [p.value for p in outcome_series if p.day not in exposure_days]

    if len(with_vals) < min_days or len(without_vals) < min_days:
        return Observation(
            exposure=exposure, outcome=outcome, lag=lag,
            days_with=len(with_vals), days_without=len(without_vals),
            enough=False,
        )

    mw, mo = _mean(with_vals), _mean(without_vals)
    diff = mw - mo
    better_up = outcome in HIGHER_IS_BETTER
    return Observation(
        exposure=exposure, outcome=outcome, lag=lag,
        days_with=len(with_vals), days_without=len(without_vals),
        mean_with=round(mw, 2), mean_without=round(mo, 2),
        enough=True, difference=round(diff, 2),
        favourable=(diff > 0) if better_up else (diff < 0),
        contributing_days=tuple(
            sorted(p.day for p in outcome_series if p.day in exposure_days)
        ),
    )


def content_hash(observations: list[Observation]) -> str:
    """Stable identity for a set of cards — same records, same hash.

    Mirrors `app/insights/core.py`: identical inputs must not produce a new
    version of the same finding.
    """
    payload = [
        [o.exposure, o.outcome, o.lag, o.days_with, o.days_without,
         o.mean_with, o.mean_without, o.enough]
        for o in sorted(observations, key=lambda x: x.key)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

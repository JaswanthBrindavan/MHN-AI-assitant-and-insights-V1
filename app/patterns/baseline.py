"""Screen 5 — "Worth a look". A reading that has moved against its OWN baseline.

THE DISTINCTION THAT MAKES THIS ALLOWED. Everywhere else in this product a
wearable number is never graded, because Davi has no reference ranges for
sleep or HRV and inventing them would be inventing clinical content. This is
not that. "Above YOUR baseline for three days" compares the reader to
themselves, states the two figures, and draws no line anyone has to agree
with. It is the difference between "your resting heart rate is high", which
claims a norm, and "your resting heart rate has averaged 71 bpm against your
usual 62", which is arithmetic on their own record.

The design's own copy makes the same distinction and it is kept verbatim in
spirit: "This isn't a diagnosis — it may be worth mentioning to a doctor."

PURE. Callers pass the series in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.patterns.core import DayValue

#: Days of baseline needed before a deviation means anything. Fewer than this
#: and "your usual" is not a usual, it is a coincidence.
MIN_BASELINE_DAYS = 14

#: Consecutive recent days that must all sit outside the baseline band. Three
#: is the design's own figure, and a run matters more than a single day: one
#: bad night moves a mean, three in a row is a direction.
RUN_DAYS = 3

#: How far from the baseline mean counts as "moved", as a share of it. A
#: personal band rather than a clinical one — see the module docstring.
DEVIATION = 0.08

#: Only metrics where a sustained move is worth a reader's attention at all.
#: Steps are deliberately absent: a quiet week is not a health signal.
WATCHED: dict[str, tuple[str, str, bool]] = {
    # key: (label, unit, higher_is_the_concerning_direction)
    "heart_rate_resting": ("resting heart rate", "bpm", True),
    "heart_rate_variability_sdnn": ("HRV", "ms", False),
    "sleep_duration": ("sleep", "min", False),
}


@dataclass(frozen=True)
class Deviation:
    metric: str
    label: str
    unit: str
    recent_mean: float
    baseline_mean: float
    run_days: int
    direction: str          # "above" | "below"
    #: The last seven days, for the chart the design draws.
    recent: tuple[DayValue, ...] = ()

    @property
    def headline(self) -> str:
        return (
            f"Your {self.label} has averaged {_fmt(self.metric, self.recent_mean)} "
            f"over the last {self.run_days} days, {self.direction} your usual "
            f"{_fmt(self.metric, self.baseline_mean)}."
        )

    @property
    def note(self) -> str:
        # The design's own wording, and the reason this card is allowed to
        # exist at all: it points at a doctor rather than at a conclusion.
        return (
            "This isn't a diagnosis and a wearable is not a medical device — "
            "it may simply be worth mentioning to a doctor."
        )


def _fmt(metric: str, value: float) -> str:
    if metric == "sleep_duration":
        return f"{value / 60:.1f} h"
    unit = WATCHED.get(metric, ("", "", True))[1]
    return f"{round(value)} {unit}".strip()


def detect(metric: str, series: list[DayValue], *, today: date | None = None):
    """A sustained move against the reader's own baseline, or None.

    The baseline EXCLUDES the recent run: comparing a run against a mean it is
    itself part of drags the mean toward the run and hides exactly the change
    the card exists to notice.
    """
    if metric not in WATCHED or len(series) < MIN_BASELINE_DAYS + RUN_DAYS:
        return None

    ordered = sorted(series, key=lambda p: p.day)
    recent, baseline = ordered[-RUN_DAYS:], ordered[:-RUN_DAYS]
    if len(baseline) < MIN_BASELINE_DAYS:
        return None

    base_mean = sum(p.value for p in baseline) / len(baseline)
    if base_mean <= 0:
        return None
    band = base_mean * DEVIATION

    above = all(p.value > base_mean + band for p in recent)
    below = all(p.value < base_mean - band for p in recent)
    if not (above or below):
        return None

    label, unit, concerning_is_up = WATCHED[metric]
    # Only surface the direction that is worth a look. An HRV rising or a
    # resting heart rate falling is not something to raise with anyone.
    if above is not concerning_is_up:
        return None

    return Deviation(
        metric=metric, label=label, unit=unit,
        recent_mean=round(sum(p.value for p in recent) / len(recent), 1),
        baseline_mean=round(base_mean, 1),
        run_days=RUN_DAYS,
        direction="above" if above else "below",
        recent=tuple(ordered[-7:]),
    )


def to_card(d: Deviation) -> dict:
    """The shape screen 5 renders."""
    return {
        "metric": d.metric,
        "title": f"Your {d.label} has moved",
        "headline": d.headline,
        "note": d.note,
        "recent_mean": d.recent_mean,
        "baseline_mean": d.baseline_mean,
        "run_days": d.run_days,
        "direction": d.direction,
        "unit": d.unit,
        # "Last 7 days vs baseline" — the chart, with the line to draw it
        # against. Absent days are simply not in the list; the client must not
        # zero-fill them.
        "series": [
            {"day": p.day.isoformat(), "value": round(p.value, 1)}
            for p in d.recent
        ],
        "baseline": d.baseline_mean,
    }

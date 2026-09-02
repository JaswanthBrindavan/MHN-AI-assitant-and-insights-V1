"""PURE co-occurrence readout: one logged habit beside one wearable reading.

Stdlib only, no I/O, no LLM — the same contract as ``app.insights.core`` and
``app.chat.memory``, and for the same reason: this is the layer that decides
what the reader's own records support, so it has to be reproducible and
readable on its own.

**What this is not.** Not a correlation coefficient, not a p-value, not
causation. Two group means over at most 28 days, self-reported on one side and
device-derived on the other, with no controls and no confounders. Every number
it produces is a description of what sits next to what in the record, and the
wording says so in the same sentence — see ``render_co_occurrence``.

Three rules hold the whole module up:

* **A day with no reading is not a zero.** Only days the device actually
  measured enter either group; days with no lifestyle row are days the reader
  did not LOG the thing, which is what the sentence says out loud.
* **Refusing is a feature.** Below ``MIN_DAYS_PER_GROUP`` days on either side
  there is no finding, only counts — a pattern over three days is noise with a
  sentence attached.
* **The wording is non-causal and stays that way.** No "because", no
  "affecting", no advice to change anything. If you are editing
  ``render_co_occurrence``,
  the test that guards this is ``tests/test_correlation.py``.

**Why this is allowed at all.** The blanket ban in
``project_docs/chat-visual-payload-contract.md`` §7 ("a causal claim, or a
statistical correlation") would have forbidden this too, and was amended
deliberately to carve it out: *"The original blanket ban would also have
forbidden this, which is stricter than intended: the harm is in the
inference, not in showing someone two of their own records side by side."*
Read that carve-out before widening anything here -- it is what the feature
stands on, and it is narrow. It permits arithmetic over the reader's own
records with no model call, a refusal below a minimum number of overlapping
days, never a medication, and never a recommendation to change anything.
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass
from datetime import date

# 28 days: four whole weeks, so both groups can contain weekdays and weekends
# rather than one group being "the reader's Saturdays". Longer would drag in
# months-old habits; shorter cannot fill two groups at the minimum below.
WINDOW_DAYS = 28

# The refusal threshold, and the only reason this module is defensible.
#
# Seven days per group, both sides, out of 28. Seven is a full week, so neither
# group can be a single weekend, and one unusual night can move a group mean by
# at most one seventh of its own deviation. It is NOT a power calculation and
# does not pretend to be one: it is the smallest count at which reporting a
# difference between two means is honest reporting rather than pattern-matching
# on noise. Below it the answer is "I do not have enough days to say", which is
# a real answer.
MIN_DAYS_PER_GROUP = 7

# What to say when the READ failed rather than the evidence being thin.
#
# Falling through to the model instead would undo the whole slot: a model asked
# "does coffee affect my sleep" answers it, causally, from its own weights, and
# it is precisely because nothing else may answer these that the handler sits
# in the shared prologue. A broken lookup is a broken lookup, not an opening.
READ_FAILED = (
    "I could not read your logs and device readings just now, so I cannot "
    "compare them — that is a problem on my side rather than anything missing "
    "from your records. Please try again in a moment."
)


@dataclass(frozen=True)
class CoOccurrence:
    """What the record shows for one (habit, wearable metric) pair.

    ``mean_with`` / ``mean_without`` are in the metric's STORED unit (sleep in
    minutes) and are ``None`` when there were not enough days to average.
    """

    input_key: str          # lifestyle log_type: coffee | tea | alcohol | water
    outcome_metric: str     # sahha metric key
    window_days: int
    days_with: int          # days the device measured AND the habit was logged
    days_without: int       # days the device measured and it was NOT logged
    mean_with: float | None
    mean_without: float | None

    @property
    def enough(self) -> bool:
        return self.mean_with is not None and self.mean_without is not None

    @property
    def measured_days(self) -> int:
        """Days that entered the comparison at all."""
        return self.days_with + self.days_without

    @property
    def difference(self) -> float:
        """``mean_with - mean_without``; 0.0 when there is no finding."""
        if self.mean_with is None or self.mean_without is None:
            return 0.0
        return self.mean_with - self.mean_without


def co_occurrence(
    input_key: str,
    outcome_metric: str,
    logged_days: Set[date],
    outcome: Mapping[date, float],
    window_days: int = WINDOW_DAYS,
) -> CoOccurrence:
    """Split the measured days by whether the habit was logged, and average.

    ``outcome`` is day -> headline reading; a day absent from it was never
    measured and enters NEITHER group. ``logged_days`` are the days holding a
    lifestyle row of this type — absence there means "not logged", which is a
    weaker statement than "did not happen" and is why the rendered sentence
    says "you logged" and not "you had".

    Always returns a result. ``enough`` is False when either group is short,
    and the counts are still populated so the caller can say WHY it refused.

    The THRESHOLD is a module constant and not a parameter: the refusal
    sentence names ``MIN_DAYS_PER_GROUP`` out loud, so a caller-supplied
    override would have the text promise one number while the check used
    another.

    ``window_days`` IS a parameter, because the caller may legitimately have a
    shorter one: it clamps the window at the reader's first tracked day, so
    days before they had the feature enter neither group. The rendered
    sentence quotes this number ("in the past 28 days"), so passing it is what
    keeps the text describing the window that was actually read.
    """
    with_vals = [v for d, v in sorted(outcome.items()) if d in logged_days]
    without_vals = [v for d, v in sorted(outcome.items()) if d not in logged_days]
    ok = (
        len(with_vals) >= MIN_DAYS_PER_GROUP
        and len(without_vals) >= MIN_DAYS_PER_GROUP
    )
    return CoOccurrence(
        input_key=input_key,
        outcome_metric=outcome_metric,
        window_days=window_days,
        days_with=len(with_vals),
        days_without=len(without_vals),
        mean_with=sum(with_vals) / len(with_vals) if ok else None,
        mean_without=sum(without_vals) / len(without_vals) if ok else None,
    )


def _amount(diff: float, unit: str) -> tuple[str, bool]:
    """The size of the gap in reader-facing words, and whether it rounds away.

    Sleep is stored in minutes and reported in minutes: "42 minutes less" is
    the honest resolution, where "0.7 h less" reads like instrument precision
    over a self-reported split.
    """
    if unit == "minute":
        n = round(abs(diff))
        return f"{n} minute{'' if n == 1 else 's'}", n == 0
    if unit == "count":
        # Steps. "1,200 count more" is not English; the label already says
        # what is being counted.
        n = round(abs(diff))
        return f"{n:,}", n == 0
    n = round(abs(diff), 1)
    return f"{n:g} {unit}".strip(), n == 0


def render_co_occurrence(finding: CoOccurrence, *, label: str, unit: str) -> str:
    """The reader-facing sentence. Non-causal by construction.

    ``label``/``unit`` come from the wearable catalogue
    (``app.coredata.service.sahha_meta``) so this module never carries a second
    copy of it.

    Every branch states the counts, because the counts are the finding. The
    caveat clause is not decoration and must not be trimmed: two group means
    over four weeks of self-reported logs cannot separate a habit from
    everything that travels with it.
    """
    habit = finding.input_key
    window = finding.window_days
    if finding.measured_days == 0:
        return (
            f"I do not have enough days to say. In the past {window} days I "
            f"have no {label} readings from a connected device to line up "
            f"against your {habit} log, so there is nothing to compare."
        )
    if not finding.enough:
        return (
            f"I do not have enough days to say. Of the {finding.measured_days} "
            f"days in the past {window} where your device recorded {label}, "
            f"you logged {habit} on {finding.days_with} and did not on "
            f"{finding.days_without}. I would want at least "
            f"{MIN_DAYS_PER_GROUP} days of each before putting two of your "
            "records side by side — fewer than that and a single unusual day "
            "sets the whole picture."
        )
    text, negligible = _amount(finding.difference, unit)
    if negligible:
        middle = (
            f"your recorded {label} averaged about the same as on the "
            f"{finding.days_without} days you did not"
        )
    else:
        lower, higher = {
            "minute": ("less", "more"),
            "count": ("fewer", "more"),
        }.get(unit, ("lower", "higher"))
        direction = higher if finding.difference > 0 else lower
        middle = (
            f"your recorded {label} averaged {text} {direction} than on the "
            f"{finding.days_without} days you did not"
        )
    return (
        f"On the {finding.days_with} days in the past {window} that you logged "
        f"{habit}, {middle}. That is what your records show together; it is "
        "not evidence that one caused the other, and many other things differ "
        "between those days."
    )

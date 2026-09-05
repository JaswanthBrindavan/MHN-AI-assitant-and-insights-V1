"""Yesterday, in two lines.

The home screen of every client shows a "Yesterday at a glance" card. It used
to read "Yesterday you had 1.8 L, 3 cups and 1 drink" — three figures with
nothing said about them, which is a table wearing a sentence's clothes. The
reader can already see those numbers on the trackers themselves; what they
cannot see is whether the day was, on balance, a good one.

**Why this is here and not in the app.** Three clients render this card, and a
ladder of rules plus a page of copy would have been written three times and
drifted three ways. It is also almost entirely *wording*, and wording that
lives in a client can only be changed by shipping a release to an app store.
The correlation cards moved here for the same reason — see ``render.py``.

The ladder
----------

Tried top down; the FIRST rung with something to say wins. The order is
clinical rather than technical: a symptom somebody typed outranks anything a
wrist strap measured, and a resting heart rate that moved outranks a step count
that did.

1. symptoms and health events
2. vitals that moved — resting heart rate, HRV, oxygen saturation
3. two things that moved together on the same day
4. sleep and recovery
5. activity
6. lifestyle — water, caffeine, alcohol

When no rung fires the day was ordinary, and :func:`_steady_day` says exactly
that. **Not forcing an insight is a feature.** A card that finds something
profound in every Tuesday teaches the reader to stop believing it, and on the
day it has something real to say they will scroll past that too.

Two rules that are not style
----------------------------

**Nothing is said about a number that is not there.** Every clause is built
from a non-null :class:`DaySignal`, so a reader with no wearable gets a shorter
sentence rather than an invented one. There is no default and no placeholder.

**Nothing here claims a cause.** Rung 3 is the one that pairs two measures, and
it joins them with "and", "after" or "alongside" — never "because". Higher
caffeine and shorter sleep on one day is a coincidence worth noticing and
nothing more; ``/patterns/correlations`` exists precisely because answering
that question takes weeks of days rather than one.

Saying which measures it rested on
----------------------------------

The home card shows the verdict; tapping through opens the review, which
repeats the reasoning and then draws a chart per measure behind it. Only the
ladder knows which those are — the rung that fired consulted them — and a
client cannot recover them from the copy without reading the wording back out
of it, which breaks the first time a sentence here is reworded. So each
summary carries :attr:`YesterdaySummary.drivers`, the metric keys themselves,
in the vocabulary the daily series already use.

Empty is a real answer there. A day carried by a reported symptom rests on
nothing anybody can plot — there is no chart of a headache — and a review
screen filled with charts of whatever else happened to be recorded would
answer a question the reader did not ask.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "DaySignal",
    "Trend",
    "YesterdayFacts",
    "YesterdaySummary",
    "baseline_of",
    "day_signal",
    "summarise_yesterday",
]


class Trend(StrEnum):
    """Where yesterday's figure sits against the days before it."""

    BELOW = "below"
    USUAL = "usual"
    ABOVE = "above"


@dataclass(frozen=True)
class DaySignal:
    """One measurement of yesterday, and the run of days it is judged against.

    ``baseline`` is None when there is not enough history to have an opinion —
    a first week, or a tracker used twice. The value still prints; only the
    comparison is withheld.
    """

    value: float
    baseline: float | None
    trend: Trend

    @property
    def delta(self) -> float | None:
        """Signed, in the signal's own unit. None when there is no baseline."""
        return None if self.baseline is None else self.value - self.baseline

    @property
    def notable(self) -> bool:
        return self.trend is not Trend.USUAL


def day_signal(value: float, baseline: float | None, *, min_change: float) -> DaySignal:
    """Build a signal, deciding the trend from how far ``value`` sits out.

    ``min_change`` is an absolute floor in the signal's own unit, and it is the
    whole reason this is not a percentage. Eleven steps more than usual is a
    rounding artefact; three beats per minute on a resting heart rate is not,
    and the two cannot share a threshold.
    """
    if baseline is None:
        trend = Trend.USUAL
    elif value - baseline >= min_change:
        trend = Trend.ABOVE
    elif baseline - value >= min_change:
        trend = Trend.BELOW
    else:
        trend = Trend.USUAL
    return DaySignal(value=value, baseline=baseline, trend=trend)


def baseline_of(days: list[float | None], *, min_days: int = 3) -> float | None:
    """The mean of the days behind yesterday, or None if too few carry a figure.

    Blanks are dropped rather than counted as zero: a day a tracker went
    untouched is a day with no reading, and averaging it in as 0 drags every
    baseline down until yesterday looks remarkable for being ordinary.

    Three days minimum. Two is an anecdote, and calling it "your usual" in the
    reader's own summary is the kind of overclaim this card exists to avoid.
    """
    known = [d for d in days if d is not None and d > 0]
    if len(known) < min_days:
        return None
    return sum(known) / len(known)


@dataclass(frozen=True)
class YesterdayFacts:
    """Everything the card may talk about. Absent means unrecorded, never zero.

    ``symptoms`` are display phrases already in the reader's own words, most
    important first — see ``service.gather_yesterday`` for how the two symptom
    sources are ordered and de-duplicated.
    """

    symptoms: tuple[str, ...] = ()
    sleep_minutes: DaySignal | None = None
    steps: DaySignal | None = None
    resting_heart_rate: DaySignal | None = None
    hrv: DaySignal | None = None
    spo2: DaySignal | None = None
    water_ml: DaySignal | None = None
    caffeine_cups: DaySignal | None = None
    alcohol_ml: DaySignal | None = None
    #: 1–10, the product's own mood scale.
    mood: DaySignal | None = None

    @property
    def _signals(self) -> list[DaySignal]:
        return [
            s for s in (
                self.sleep_minutes, self.steps, self.resting_heart_rate, self.hrv,
                self.spo2, self.water_ml, self.caffeine_cups, self.alcohol_ml,
                self.mood,
            ) if s is not None
        ]

    @property
    def has_anything(self) -> bool:
        """Whether there is anything at all to write about."""
        return bool(self.symptoms) or bool(self._signals)


@dataclass(frozen=True)
class YesterdaySummary:
    """The two lines. ``headline`` says what kind of day; ``detail`` the figures.

    ``drivers`` names the measures the headline rests on, as the metric keys of
    ``service.YESTERDAY_METRICS`` and ``service.YESTERDAY_HABITS`` — the same
    keys the daily series are read under, so a client can map one to a chart it
    already draws. Only what the sentences actually rest on is listed: a figure
    printed for context is not a reason, and charting it would answer a
    question the card did not raise.
    """

    headline: str
    detail: str
    #: Empty means nothing behind this headline can be plotted, which is a
    #: finding rather than a gap — see the module docstring.
    drivers: tuple[str, ...] = ()

    @property
    def heading(self) -> str:
        """The verdict on its own — the one line the home card shows."""
        return _sentences(self.headline)[0]

    @property
    def reasoning(self) -> tuple[str, ...]:
        """The sentences that belong under that heading, in reading order.

        Derived rather than written a second time. Everything after the verdict
        is already the why, so a separately authored reasoning string would be
        the same copy maintained twice, and it would drift the day somebody
        reworded a rung and edited only one of them. It also inherits the
        no-cause rule for free: these are the very sentences the suite already
        reads when it checks that no rung says "because".
        """
        return (*_sentences(self.headline)[1:], self.detail)


def summarise_yesterday(facts: YesterdayFacts) -> YesterdaySummary | None:
    """The ladder. None when nothing was recorded — the card is then not drawn."""
    if not facts.has_anything:
        return None
    for rung in _LADDER:
        said = rung(facts)
        if said is not None:
            return said
    return _steady_day(facts)


# --------------------------------------------------------------------------- #
# 1. Symptoms
# --------------------------------------------------------------------------- #

def _symptom_rung(f: YesterdayFacts) -> YesterdaySummary | None:
    """Anything the reader said outranks anything measured for them.

    A wrist strap reports a number; a person reporting a headache is telling
    you the day went badly, and no step count overrules that.
    """
    if not f.symptoms:
        return None

    named = _join([_with_article(s) for s in f.symptoms])
    if len(f.symptoms) == 1:
        headline = f"Yesterday was mostly stable, but you reported {named}."
    else:
        headline = f"Yesterday was a tougher day. You reported {named}."
    detail, drivers = _symptom_context(f)
    return YesterdaySummary(headline, detail, drivers)


def _symptom_context(f: YesterdayFacts) -> tuple[str, tuple[str, ...]]:
    """What else was low on a day somebody felt unwell, and what that charts as.

    Only what actually was low. The temptation is to explain the symptom, and
    the honest line is narrower: these were also below their usual, said next
    to each other, with no arrow drawn between them.

    The drivers are those same measures and nothing else. The fallback clause
    prints yesterday's figures to avoid an empty line, but a figure shown for
    company is not a reason the day went the way it did, and the symptom itself
    is not a series anybody can draw — so that day charts nothing.
    """
    low: list[tuple[str, str]] = []
    if f.sleep_minutes is not None and f.sleep_minutes.trend is Trend.BELOW:
        low.append(("sleep", "sleep_duration"))
    if f.steps is not None and f.steps.trend is Trend.BELOW:
        low.append(("activity", "steps"))
    if f.water_ml is not None and f.water_ml.trend is Trend.BELOW:
        low.append(("hydration", "water"))

    named = [word for word, _ in low]
    drivers = tuple(key for _, key in low)
    if len(low) >= 2:
        return f"Your {_join(named)} were all below your usual levels.", drivers
    if len(low) == 1:
        return f"Your {named[0]} was also below your usual level.", drivers
    figures = _figures(f)
    return (
        f"{_sentence_case(figures)}." if figures
        else "Nothing else you track looked unusual."
    ), ()


# --------------------------------------------------------------------------- #
# 2. Vitals that moved
# --------------------------------------------------------------------------- #

def _vital_rung(f: YesterdayFacts) -> YesterdaySummary | None:
    """Resting heart rate, HRV and oxygen saturation — meaningful on their own.

    HRV and resting heart rate are read together as recovery rather than
    reported one by one: HRV up and resting heart rate down are the same news
    said twice, and a card with two lines cannot afford to say it twice.
    """
    # An oxygen saturation drop is the one vital here that is not about
    # recovery, and it outranks the other two: it is closer to a health event
    # than to a training signal.
    if f.spo2 is not None and f.spo2.trend is Trend.BELOW:
        figures = _figures(f)
        rest = f", and {figures}" if figures else ""
        return YesterdaySummary(
            "Yesterday your oxygen saturation read lower than your recent average.",
            f"It averaged {round(f.spo2.value)}%{rest}.",
            ("spo2",),
        )

    better = [
        (phrase, key) for phrase, key, ok in (
            ("HRV was higher", "heart_rate_variability_sdnn",
             f.hrv is not None and f.hrv.trend is Trend.ABOVE),
            ("resting heart rate was lower", "heart_rate_resting",
             f.resting_heart_rate is not None
             and f.resting_heart_rate.trend is Trend.BELOW),
        ) if ok
    ]
    worse = [
        (phrase, key) for phrase, key, ok in (
            ("HRV was lower", "heart_rate_variability_sdnn",
             f.hrv is not None and f.hrv.trend is Trend.BELOW),
            ("resting heart rate was higher", "heart_rate_resting",
             f.resting_heart_rate is not None
             and f.resting_heart_rate.trend is Trend.ABOVE),
        ) if ok
    ]

    # Both directions at once is not a story, it is noise. Say nothing.
    if better and worse:
        return None

    figures = _figures(f)
    detail = f"{_sentence_case(figures)}." if figures else "Everything else looked usual."

    moved = better or worse
    said = _join([phrase for phrase, _ in moved])
    drivers = tuple(key for _, key in moved)

    if better:
        return YesterdaySummary(
            f"Your recovery looked better yesterday. {_sentence_case(said)} "
            "than your recent baseline.",
            detail,
            drivers,
        )
    if worse:
        return YesterdaySummary(
            "Yesterday looked like a harder day for recovery. "
            f"{_sentence_case(said)} than your recent baseline.",
            detail,
            drivers,
        )
    return None


# --------------------------------------------------------------------------- #
# 3. Two things that moved together
# --------------------------------------------------------------------------- #

def _together_rung(f: YesterdayFacts) -> YesterdaySummary | None:
    """One day, two measures, and no arrow between them.

    Every phrase here is deliberately weak — "and", "after", "alongside". A
    single day cannot separate the two orderings, let alone rule out the third
    thing that moved both, and ``/patterns/correlations`` exists because
    answering that takes weeks of days rather than one.
    """
    sleep = f.sleep_minutes if (
        f.sleep_minutes is not None and f.sleep_minutes.trend is Trend.BELOW
    ) else None

    if sleep is not None:
        if f.caffeine_cups is not None and f.caffeine_cups.trend is Trend.ABOVE:
            return YesterdaySummary(
                "Yesterday was a little off. Your sleep was shorter than usual, "
                "and you had more caffeine than your typical intake.",
                f"You slept {_duration(sleep.value)} after "
                f"{_cups(f.caffeine_cups.value)}.",
                ("sleep_duration", "coffee"),
            )

        if f.alcohol_ml is not None and f.alcohol_ml.trend is Trend.ABOVE:
            return YesterdaySummary(
                "Yesterday was a little off. You slept less than usual, alongside "
                "more alcohol than you typically log.",
                f"You slept {_duration(sleep.value)} after "
                f"{_drinks(f.alcohol_ml.value)}.",
                ("sleep_duration", "alcohol"),
            )

        if f.mood is not None and f.mood.trend is Trend.BELOW:
            walked = f", and walked {_steps(f.steps.value)}" if f.steps else ""
            return YesterdaySummary(
                "Yesterday looked like a flat day. Your sleep was shorter than usual "
                "and your mood was lower than you usually log.",
                f"You slept {_duration(sleep.value)}{walked}.",
                ("sleep_duration", "mood"),
            )

        if f.steps is not None and f.steps.trend is Trend.BELOW:
            figures = _figures(f)
            return YesterdaySummary(
                "Yesterday was a quieter day. You slept less than usual and moved "
                "less than usual too.",
                f"{_sentence_case(figures)}." if figures
                else f"You slept {_duration(sleep.value)}.",
                ("sleep_duration", "steps"),
            )

    if (
        f.water_ml is not None and f.water_ml.trend is Trend.BELOW
        and f.mood is not None and f.mood.trend is Trend.BELOW
    ):
        slept = (
            f" and slept {_duration(f.sleep_minutes.value)}"
            if f.sleep_minutes is not None else ""
        )
        return YesterdaySummary(
            "Yesterday was a little off. Your hydration and your mood were both "
            "below your usual levels.",
            f"You logged {_litres(f.water_ml.value)} of water{slept}.",
            ("water", "mood"),
        )

    return None


# --------------------------------------------------------------------------- #
# 4. Sleep and recovery
# --------------------------------------------------------------------------- #

def _sleep_rung(f: YesterdayFacts) -> YesterdaySummary | None:
    sleep = f.sleep_minutes
    if sleep is None or not sleep.notable:
        return None

    gap = abs(sleep.delta) if sleep.delta is not None else None
    by = ""
    if gap is not None and gap >= 1:
        way = "above" if sleep.trend is Trend.ABOVE else "below"
        by = f", about {_about_minutes(gap)} {way} your recent average"

    if sleep.trend is Trend.ABOVE:
        # The heart-rate clause is claimed as a driver only when it was
        # actually written. It is dropped whenever there is nothing to compare
        # against, and a chart of a reading the sentence never mentioned is the
        # same overclaim in a second medium.
        steady = _steady_heart_rate(f)
        return YesterdaySummary(
            f"Yesterday was a good recovery day. You slept better than usual"
            f"{steady}.",
            f"You got {_duration(sleep.value)} of sleep{by}.",
            ("sleep_duration", "heart_rate_resting") if steady
            else ("sleep_duration",),
        )
    return YesterdaySummary(
        "Yesterday your sleep was shorter than usual.",
        f"You got {_duration(sleep.value)} of sleep{by}.",
        ("sleep_duration",),
    )


def _steady_heart_rate(f: YesterdayFacts) -> str:
    """Only when it really did hold steady — otherwise the clause is left off.

    A USUAL trend with no baseline means "nothing to compare against", not
    "stable". Calling that stable is invented reassurance.
    """
    rhr = f.resting_heart_rate
    if rhr is not None and rhr.trend is Trend.USUAL and rhr.baseline is not None:
        return " and your resting heart rate remained stable"
    return ""


# --------------------------------------------------------------------------- #
# 5. Activity
# --------------------------------------------------------------------------- #

def _activity_rung(f: YesterdayFacts) -> YesterdaySummary | None:
    steps = f.steps if (f.steps is not None and f.steps.notable) else None
    if steps is None:
        return None

    figures = _figures(f)
    return YesterdaySummary(
        "You were more active yesterday than usual." if steps.trend is Trend.ABOVE
        else "Yesterday was a quieter day for movement than usual.",
        f"{_sentence_case(figures)}." if figures
        else f"You walked {_steps(steps.value)}.",
        ("steps",),
    )


# --------------------------------------------------------------------------- #
# 6. Lifestyle
# --------------------------------------------------------------------------- #

def _lifestyle_rung(f: YesterdayFacts) -> YesterdaySummary | None:
    slept = (
        f", with {_duration(f.sleep_minutes.value)} of sleep"
        if f.sleep_minutes is not None else ""
    )

    caffeine = f.caffeine_cups if (
        f.caffeine_cups is not None and f.caffeine_cups.notable
    ) else None
    if caffeine is not None:
        more = "more" if caffeine.trend is Trend.ABOVE else "less"
        return YesterdaySummary(
            f"Yesterday you had {more} caffeine than your typical intake.",
            f"That was {_cups(caffeine.value)}{slept}.",
            ("coffee",),
        )

    alcohol = f.alcohol_ml if (
        f.alcohol_ml is not None and f.alcohol_ml.notable
    ) else None
    if alcohol is not None:
        more = "more" if alcohol.trend is Trend.ABOVE else "less"
        return YesterdaySummary(
            f"Yesterday you logged {more} alcohol than usual.",
            f"That was {_drinks(alcohol.value)}{slept}.",
            ("alcohol",),
        )

    water = f.water_ml if (f.water_ml is not None and f.water_ml.notable) else None
    if water is not None:
        way = "above" if water.trend is Trend.ABOVE else "below"
        return YesterdaySummary(
            f"Your hydration was {way} your usual level yesterday.",
            f"You logged {_litres(water.value)} of water.",
            ("water",),
        )

    return None


_LADDER = (
    _symptom_rung,
    _vital_rung,
    _together_rung,
    _sleep_rung,
    _activity_rung,
    _lifestyle_rung,
)


# --------------------------------------------------------------------------- #
# The ordinary day
# --------------------------------------------------------------------------- #

def _steady_day(f: YesterdayFacts) -> YesterdaySummary:
    """Nothing moved, and that is the finding.

    Names only what was actually recorded, so a reader with no wearable is not
    told their vitals were steady. "No major changes" about three trackers is
    true; about a wearable that was never worn it is a small lie they have no
    way to catch.
    """
    recorded: list[str] = []
    # A group is named as one word and charts as every series under it: "your
    # recorded vitals were close to usual" is a claim about each of them, and
    # the reader who wants to see that gets the flat lines rather than one
    # arbitrary member of the group.
    drivers: list[str] = []
    if f.sleep_minutes is not None:
        recorded.append("sleep")
        drivers.append("sleep_duration")
    if f.steps is not None:
        recorded.append("activity")
        drivers.append("steps")
    vitals = [
        key for key, signal in (
            ("heart_rate_resting", f.resting_heart_rate),
            ("heart_rate_variability_sdnn", f.hrv),
            ("spo2", f.spo2),
        ) if signal is not None
    ]
    if vitals:
        recorded.append("recorded vitals")
        drivers.extend(vitals)
    logs = [
        key for key, signal in (
            ("water", f.water_ml),
            ("coffee", f.caffeine_cups),
            ("alcohol", f.alcohol_ml),
        ) if signal is not None
    ]
    if logs:
        recorded.append("daily logs")
        drivers.extend(logs)

    if len(recorded) >= 2:
        detail = f"Your {_join(recorded)} were all close to your usual levels."
    elif len(recorded) == 1:
        detail = f"Your {recorded[0]} stayed close to your usual level."
    else:
        # Nothing was named, so nothing is charted — a mood score on its own
        # reaches here, and the line does not claim to be about it.
        detail = "Nothing you track moved much."
        drivers = []
    return YesterdaySummary(
        "Yesterday was a steady day with no major changes.", detail, tuple(drivers)
    )


# --------------------------------------------------------------------------- #
# Saying the numbers
# --------------------------------------------------------------------------- #

def _figures(f: YesterdayFacts) -> str | None:
    """"you got 7h 32m of sleep and 8,420 steps" — the fastest-recognised pair.

    None when neither exists, which is the caller's cue to say something else
    rather than print an empty clause.
    """
    parts = []
    if f.sleep_minutes is not None:
        parts.append(f"{_duration(f.sleep_minutes.value)} of sleep")
    if f.steps is not None:
        parts.append(_steps(f.steps.value))
    if not parts:
        return None
    tail = "" if f.symptoms else ", with no new symptoms reported"
    return f"you got {_join(parts)}{tail}"


def _duration(minutes: float) -> str:
    """Minutes as "7h 20m".

    Under an hour drops the hours rather than writing "0h 40m", which reads
    like a fault in the formatter rather than a short night.
    """
    total = max(0, round(minutes))
    hours, mins = divmod(total, 60)
    return f"{mins}m" if hours == 0 else f"{hours}h {mins}m"


def _about_minutes(minutes: float) -> str:
    """A gap, rounded the way somebody would say it out loud."""
    total = round(minutes)
    if total < 60:
        return f"{total} minutes"
    hours, mins = divmod(total, 60)
    head = "1 hour" if hours == 1 else f"{hours} hours"
    return head if mins == 0 else f"{head} {mins}m"


def _steps(value: float) -> str:
    return f"{round(value):,} steps"


def _cups(value: float) -> str:
    n = round(value)
    return "1 cup of coffee" if n == 1 else f"{n} cups of coffee"


#: Millilitres of pure alcohol in one standard drink. The clients divide by the
#: same constant before showing a count, and this card must agree with them.
STANDARD_DRINK_ML = 17.7


def _drinks(ml: float) -> str:
    n = ml / STANDARD_DRINK_ML
    text = f"{round(n)}" if abs(n - round(n)) < 0.05 else f"{n:.1f}"
    return f"{text} drink" if abs(n - 1.0) < 0.05 else f"{text} drinks"


def _litres(ml: float) -> str:
    litres = ml / 1000.0
    text = f"{litres:.2f}".rstrip("0").rstrip(".") or "0"
    return f"{text} L"


#: Countable symptoms take an article; everything else does not.
#:
#: Deliberately the safer way round. An unknown phrase printed without an
#: article reads a little bare — "you reported dizziness" — while the same
#: phrase printed WITH one can read as nonsense: "you reported a nausea". A
#: missing article is a blemish; a wrong one looks like a machine wrote the
#: sentence, which is exactly what this card is trying not to sound like.
_COUNTABLE = frozenset({
    "headache", "migraine", "fever", "cough", "cold", "rash", "cramp",
    "sore throat", "seizure", "nosebleed",
})


def _with_article(symptom: str) -> str:
    lower = symptom.lower()
    if lower not in _COUNTABLE:
        return lower
    return f"an {lower}" if lower[0] in "aeiou" else f"a {lower}"


def _join(items: list[str]) -> str:
    """"a", "a and b", "a, b and c" — no Oxford comma, matching the app's copy."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _sentence_case(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _sentences(text: str) -> list[str]:
    """Split a written line into its sentences.

    The whitespace after the full stop is required, so "1.8 L" and "8.5 h" stay
    whole: a split inside a figure would put half a number in the heading and
    the other half in the reasoning.
    """
    return re.split(r"(?<=\.)\s+", text.strip())

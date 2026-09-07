"""Turn an Observation into the words a reader sees. PURE, stdlib-only.

THE SENTENCE HAS THREE PARTS, and keeping them apart is the whole point:

  1. WHAT THE RECORDS SHOW — counts and an average, both from the reader's own
     data. Verifiable, and stated as an observation.
  2. THE HEDGE — "this might be the reason". The owner's own wording. It is
     weaker than "causes" and honest about what a month of one person's days
     can support.
  3. THE FACT — general, from the clinician-reviewed corpus, and clearly about
     people in general rather than about this reader.

Part 3 is where the causal knowledge lives, and it is the only part entitled to
it: the corpus IS reviewed. Part 1 stays observational. A reader gets the
useful explanation without the product claiming to have proved it about them.

NO CONFIDENCE SCORE. "Confidence: high" reads as a statistical claim we cannot
support, and the owner asked for it to be dropped. The day counts say the same
thing without the false precision.
"""

from __future__ import annotations

from app.patterns.core import (
    LAG_NEXT_DAY,
    MIN_DAYS_PER_GROUP,
    WINDOW_DAYS,
    Observation,
)

#: How the exposure is named mid-sentence.
EXPOSURE_PHRASE: dict[str, str] = {
    "coffee": "logged coffee after 3pm",
    "tea": "logged tea",
    "alcohol": "logged alcohol",
    "smoking": "logged smoking",
    "water_low": "drank less water than usual",
}

#: How the outcome reads after "your recorded ...".
OUTCOME_PHRASE: dict[str, str] = {
    "sleep_duration": "sleep",
    "steps": "step count",
    "heart_rate_resting": "resting heart rate",
    "heart_rate_variability_sdnn": "HRV",
    "blood_pressure": "systolic blood pressure",
    "blood_sugar": "blood sugar",
    "heart_rate": "heart rate",
    "mood": "mood score",
}

#: What the reader has to keep RECORDING for a pair to become answerable,
#: as an instruction rather than as the past-tense clause above. "Log your
#: coffee, with the time" is a request for a record; "have less coffee" would
#: be advice, and nothing on this screen gives any.
EXPOSURE_ASK: dict[str, str] = {
    "coffee": "log your coffee with the time you had it",
    "tea": "log your tea",
    "alcohol": "log your drinks on the days you have one",
    "smoking": "log your cigarettes on the days you smoke",
    "water_low": "log your water every day",
}

#: How the OUTCOME half gets on to the record. A wearable metric is worn, not
#: logged, and telling somebody to "log your HRV" is telling them to do a
#: thing the app cannot accept.
OUTCOME_ASK: dict[str, str] = {
    "sleep_duration": "wear your watch overnight",
    "steps": "wear your watch",
    "heart_rate": "wear your watch",
    "heart_rate_resting": "wear your watch",
    "heart_rate_variability_sdnn": "wear your watch",
    "blood_pressure": "take a blood-pressure reading",
    "blood_sugar": "take a blood-sugar reading",
    "mood": "log your mood",
}

WHEN_PHRASE = {
    "same_day": "the same day",
    "next_day": "the next day",
    "cumulative": "over the window",
}


def _amount(outcome: str, value: float) -> str:
    """The difference in the unit a person would say it in."""
    size = abs(value)
    if outcome == "sleep_duration":                     # stored in minutes
        if size >= 60:
            hours = size / 60
            return f"{hours:.1f} h".replace(".0 h", " h")
        return f"{round(size)} minutes"
    if outcome == "steps":
        return f"{round(size):,} steps"
    if outcome == "mood":
        return f"{size:.1f} points"
    unit = {
        "heart_rate_resting": "bpm", "heart_rate": "bpm",
        "heart_rate_variability_sdnn": "ms",
        "blood_pressure": "mmHg", "blood_sugar": "mg/dL", "spo2": "%",
    }.get(outcome, "")
    # One decimal. "6.56 bpm" is precision the comparison does not have.
    return f"{size:.1f} {unit}".rstrip().replace(".0 ", " ")


def headline(o: Observation, fact: str | None = None) -> str:
    """The one line the card shows. Observational, never causal.

    With no personal data yet, the card leads with the GENERAL fact rather
    than with an apology. "No patterns yet" is a dead screen; the reviewed
    corpus already has something worth reading about coffee and sleep, and it
    is true whether or not this reader has logged anything. It is prefixed
    "In general" so it can never be mistaken for a finding about them.
    """
    if not o.enough:
        return f"In general: {fact}" if fact else (
            "Not enough days yet to put these side by side."
        )
    direction = "lower" if (o.difference or 0) < 0 else "higher"
    if o.outcome == "sleep_duration":
        direction = "shorter" if (o.difference or 0) < 0 else "longer"
    when = WHEN_PHRASE.get(o.lag, "")
    if o.outcome == "sleep_duration" and o.lag != LAG_NEXT_DAY:
        when = "that night"
    return (
        f"On days you {EXPOSURE_PHRASE.get(o.exposure, o.exposure)}, your "
        f"recorded {OUTCOME_PHRASE.get(o.outcome, o.outcome)} was "
        f"{_amount(o.outcome, o.difference or 0)} {direction} "
        f"{when}.".replace("  ", " ")
    )


def detail(o: Observation, fact: str | None = None) -> str:
    """The full reading for the detail screen: observation, hedge, then fact."""
    if not o.enough:
        need = max(0, 7 - min(o.days_with, o.days_without))
        # The general half first, because it is the half that is ready. Then
        # the honest note about their own data. Never the other way round: a
        # card that opens with what it cannot do is a card nobody reads.
        opening = f"In general: {fact} " if fact else ""
        return (
            f"{opening}Whether that shows up in your own nights is a separate "
            f"question, and I cannot answer it yet. Over the last 28 days "
            f"there were {o.days_with} days with and {o.days_without} without "
            f"a reading to compare — about {need} more would do it. Fewer than "
            f"that and a single unusual day sets the whole picture."
        )

    when = "that night" if o.lag != LAG_NEXT_DAY else "the next day"
    direction = "lower" if (o.difference or 0) < 0 else "higher"
    if o.outcome == "sleep_duration":
        direction = "shorter" if (o.difference or 0) < 0 else "longer"

    body = (
        f"On the {o.days_with} days you "
        f"{EXPOSURE_PHRASE.get(o.exposure, o.exposure)}, your recorded "
        f"{OUTCOME_PHRASE.get(o.outcome, o.outcome)} averaged "
        f"{_amount(o.outcome, o.difference or 0)} {direction} {when} than on "
        f"the {o.days_without} days you did not. "
        # The hedge, in the owner's own words.
        f"This might be the reason — though many other things differ between "
        f"those days, and a month of your own days cannot separate them."
    )
    if fact:
        # The general half, clearly marked as being about people rather than
        # about this reader.
        body += f" In general: {fact}"
    return body


def to_card(o: Observation, *, title: str = "", fact: str | None = None) -> dict:
    """The shape the client renders. Presentation words, no verdicts."""
    return {
        "key": o.key,
        "title": title or f"{o.exposure} and your {o.outcome}",
        "headline": headline(o, fact),
        "detail": detail(o, fact),
        "enough_data": o.enough,
        "days_with": o.days_with,
        "days_without": o.days_without,
        "when": WHEN_PHRASE.get(o.lag, ""),
        # Signed, in the outcome's own stored unit, so the client can draw it.
        "difference": o.difference,
        "mean_with": o.mean_with,
        "mean_without": o.mean_without,
        # Whether the difference points the way the reader would rather it
        # did. A direction, NOT a grade and NOT advice.
        "favourable": o.favourable,
    }


def waiting_note(
    exposure: str,
    outcome: str,
    days_with: int,
    days_without: int,
    *,
    title: str = "",
    min_days: int = MIN_DAYS_PER_GROUP,
    window: int = WINDOW_DAYS,
) -> str:
    """What the reader has to keep doing for the nearest pair to open.

    "3 more days to unlock" on its own is a countdown with no instructions,
    and readers reasonably assume it counts down by itself. It does not: it
    moves only when BOTH halves of the comparison land on the record on the
    same day, and a reader who was never told what the two halves are can log
    diligently for a month against the wrong one and watch the number sit
    still. So name them, in the order they have to happen.

    Which half is short is worth one more clause. Somebody with 25 logged days
    and 2 without is not short of logging — they are short of days the habit
    did not happen, and "log more" is exactly the wrong thing to tell them.

    Counts are stated rather than converted into a percentage: they are small
    integers the reader can check against their own week.
    """
    ask = EXPOSURE_ASK.get(exposure, f"log your {exposure.replace('_', ' ')}")
    record = OUTCOME_ASK.get(
        outcome, f"record your {OUTCOME_PHRASE.get(outcome, outcome)}"
    )
    # Naming the pair costs a clause and turns an anonymous instruction into
    # one the reader can picture. The title is already on the card.
    opening = (
        f"{title} is the nearest one. Two things have to be on the record on "
        f"the same day: " if title else
        "Two things have to be on the record on the same day for this one: "
    )
    note = (
        f"{opening}{record}, and {ask}. Over the last {window} days you have "
        f"{days_with} with and {days_without} without a reading to compare, "
        f"and it takes {min_days} of each."
    )
    if days_without < min_days <= days_with:
        note += " The days without are the half that is short."
    elif days_with < min_days <= days_without:
        note += " The days with are the half that is short."
    return note

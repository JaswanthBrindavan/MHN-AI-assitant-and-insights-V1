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

from app.patterns.core import LAG_NEXT_DAY, Observation

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

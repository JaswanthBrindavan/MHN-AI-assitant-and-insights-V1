"""Numeric fidelity — the model may not invent or drift a clinical value.

Once tools return raw data and the MODEL composes the sentence, nothing else
stops it turning 6.1% into 6.5%. The banned-phrase validator has no opinion
about numbers, and grounding only checks that a marker is present, not that
the number behind it is right.
"""

from __future__ import annotations

from app.grounding.fidelity import digits_preserved, unit_values, values_traceable


# --------------------------------------------------------------------------- #
# digits_preserved — the translation guard
# --------------------------------------------------------------------------- #
def test_digits_preserved_accepts_a_faithful_translation():
    assert digits_preserved("call 14416 now", "14416 पर कॉल करें")


def test_digits_preserved_rejects_a_corrupted_helpline_number():
    assert not digits_preserved("call 14416", "call 1416")


def test_digits_preserved_rejects_a_dropped_dose():
    assert not digits_preserved("take 500 mg twice", "take twice")


def test_digits_preserved_is_order_sensitive():
    # Order matters now (audit medium): "take 2 of the 500mg" vs "take 500 of
    # the 2mg" share a digit multiset and only one is survivable. Legitimate
    # clause reordering pays a false trip; the dose<->strength swap is the
    # asymmetry that decides it.
    assert not digits_preserved("5 and 10", "10 and 5")
    assert not digits_preserved("5 and 10", "5 and 10 and 10")


# --------------------------------------------------------------------------- #
# unit_values — what counts as a clinical value
# --------------------------------------------------------------------------- #
def test_unit_values_picks_up_lab_values_and_bp():
    found = unit_values("HbA1c was 6.1% and BP 128/84 mmHg.")
    assert "6.1%" in found
    assert any("128/84" in v for v in found)


def test_prose_numbers_are_not_clinical_values():
    """The guard must have no opinion about ordinary writing."""
    assert unit_values("Here are 3 things to discuss at step 2.") == []


# --------------------------------------------------------------------------- #
# values_traceable — the tool-composition guard
# --------------------------------------------------------------------------- #
def test_traceable_when_every_value_came_from_a_source():
    ok, stray = values_traceable(
        "Your last HbA1c was 6.1%.", ['{"test": "HbA1c", "value": "6.1%"}']
    )
    assert ok and stray == []


def test_untraceable_when_the_model_drifts_a_value():
    ok, stray = values_traceable(
        "Your last HbA1c was 6.5%.", ['{"test": "HbA1c", "value": "6.1%"}']
    )
    assert not ok
    assert stray == ["6.5%"]


def test_reformatting_a_value_is_allowed_but_changing_it_is_not():
    """The model may tidy '128/84mmhg' into '128/84 mmHg' — that is a
    presentation change, not a claim change."""
    ok, _ = values_traceable(
        "Your reading was 128/84 mmHg.", ['{"bp": "128/84mmhg"}']
    )
    assert ok

    ok, stray = values_traceable(
        "Your reading was 138/84 mmHg.", ['{"bp": "128/84mmhg"}']
    )
    assert not ok and stray


def test_a_value_at_the_end_of_a_source_sentence_still_traces():
    """The trailing full stop is a sentence boundary, not a decimal point.

    Whitespace is stripped before comparing, so "62 bpm. That is what your
    device recorded" becomes "62bpm.thatis" -- and a boundary that rejected any
    following dot made every deterministic reply ENDING in a value untraceable.
    A vitals line, a lab line and a wearable line all end that way.
    """
    ok, stray = values_traceable(
        "Your resting heart rate was 62 bpm.",
        ["resting heart rate 62 bpm. That is what your device recorded."],
    )
    assert ok and stray == []


def test_a_value_that_is_only_the_head_of_a_longer_number_does_not_trace():
    """The boundary still does its real job."""
    ok, stray = values_traceable("You took 62 mg.", ["the dose was 620 mg"])
    assert not ok and stray == ["62 mg"]

    ok, stray = values_traceable("Your reading was 62 bpm.", ["it was 62.5 bpm"])
    assert not ok and stray == ["62 bpm"]

    ok, stray = values_traceable("You took 62 mg.", ["the dose was 1.62 mg"])
    assert not ok and stray == ["62 mg"]


def test_no_sources_means_nothing_to_check():
    """A general education answer cites no patient data."""
    ok, stray = values_traceable("Adults generally need 7-9 hours of sleep.", [])
    assert ok and stray == []


def test_a_value_repeated_wrongly_is_reported_once():
    ok, stray = values_traceable(
        "It was 9.9% — yes, 9.9% is the figure.", ['{"value": "6.1%"}']
    )
    assert not ok
    assert stray == ["9.9%"]


def test_multiple_sources_are_all_searched():
    """Tool results and retrieved chunks are pooled — a value from either is
    legitimate."""
    ok, _ = values_traceable(
        "Your HbA1c was 6.1% and the usual target is under 5.7%.",
        ['{"value": "6.1%"}', "Diagnostic threshold: 5.7% and above."],
    )
    assert ok


def test_the_guard_catches_a_wholly_invented_dose():
    """The failure mode that matters most: a plausible dose nobody supplied."""
    ok, stray = values_traceable(
        "The usual dose is 500 mg twice daily.",
        ['{"name": "Metformin", "uses": ["type 2 diabetes"]}'],
    )
    assert not ok
    assert stray == ["500 mg"]


# --------------------------------------------------------------------------- #
# Thousands separators — the reason "How's my water intake?" was answered with
# the safe reply. The model quoted the tool's own figure back with a comma; the
# comma is a word boundary, so the token was the FRAGMENT "000 ml", no source
# contained it, and the whole (verbatim-correct) reply was discarded.
# --------------------------------------------------------------------------- #
def test_a_thousands_separator_is_one_value_not_a_fragment():
    assert unit_values("You have logged 14,000 ml of water.") == ["14,000 ml"]
    assert unit_values("120,000 iu and 1,250 kg") == ["120,000 iu", "1,250 kg"]


def test_a_comma_written_figure_traces_to_an_uncomma_d_source():
    ok, stray = values_traceable(
        "You have logged 14,000 ml of water in the past 7 days.",
        ["You have logged 14000 ml of water in the past 7 days."],
    )
    assert ok and stray == []


def test_the_fragment_no_longer_launders_a_wrong_number():
    """The bug cut both ways: "1,500 mg" used to tokenise as "500 mg" and TRACE
    against a source saying 500 mg. A tenfold dose passed the guard."""
    ok, stray = values_traceable("Take 1,500 mg daily.", ["Take 500 mg daily."])
    assert not ok
    assert stray == ["1,500 mg"]


# --------------------------------------------------------------------------- #
# A figure the model DERIVED from the reader's own total.
#
# Dividing a weekly total by seven is the single most likely thing a helpful
# model does with a weekly total, and the whole reply was replaced by the safe
# reply for it: "You logged 14,000 ml over the week - roughly 2,000 ml per day"
# is verbatim-correct in both halves. The tolerance is deliberately narrow --
# same unit, a calendar divisor, and a source value that actually exists --
# because this guard is why the invented-number class is visible at all.
# --------------------------------------------------------------------------- #
_WEEK = ["You have logged 14000 ml of water in the past 7 days."]


def test_a_per_day_average_of_a_traced_total_is_allowed():
    ok, stray = values_traceable(
        "You logged 14000 ml over the week - roughly 2000 ml per day.", _WEEK
    )
    assert ok and stray == []


def test_a_rounded_average_is_allowed_but_a_re_rounded_one_is_not():
    """15000 / 7 = 2142.86. Models round; they do not round to the nearest
    thousand and call it an average."""
    fifteen = ["You have logged 15000 ml of water in the past 7 days."]
    assert values_traceable("That is about 2,140 ml a day.", fifteen)[0]
    assert not values_traceable("That is about 2,000 ml a day.", fifteen)[0]


def test_a_drifted_average_is_still_caught():
    """1,950 is not 14,000/7 and not anything else in the sources."""
    ok, stray = values_traceable(
        "You logged 14,000 ml this week, about 1,950 ml a day.", _WEEK
    )
    assert not ok and stray == ["1,950 ml"]


def test_an_invented_value_does_not_become_traceable_by_arithmetic():
    ok, stray = values_traceable(
        "Your reading was 117 mg/dl and the usual dose is 500 mg.", _WEEK
    )
    assert not ok
    assert stray == ["117 mg/dl", "500 mg"]


def test_a_blood_pressure_pair_is_never_derived():
    """No arithmetic path may launder a BP pair: it has no single magnitude."""
    ok, stray = values_traceable(
        "Your reading was 128/84 mmHg.", ["Your last reading was 130/85 mmHg."]
    )
    assert not ok and stray == ["128/84 mmHg"]

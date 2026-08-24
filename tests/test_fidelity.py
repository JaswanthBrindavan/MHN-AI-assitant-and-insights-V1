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


def test_digits_preserved_is_order_insensitive_but_count_sensitive():
    assert digits_preserved("5 and 10", "10 and 5")
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

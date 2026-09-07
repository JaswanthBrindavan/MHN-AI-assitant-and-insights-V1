"""Two pieces of pure logic that answer real questions and had no test.

`_reclassify_glucose` re-bands a remembered reading once the reader says
whether it was fasting or after a meal -- 120 mg/dL is high fasting and fine
two hours after lunch, and the wrong band is the wrong advice.

`param_aliases` / `param_tokens` decide which series a lab question opens. The
registry's hemoglobin exclusion fixed a real wrong answer on a real account:
"MEAN CORPUSCULAR HEMOGLOBIN(MCH)" was being folded into hemoglobin.
"""

from __future__ import annotations

import pytest

from app.chat.abilities import param_aliases, param_tokens
from app.chat.data_handlers import _reclassify_glucose


@pytest.mark.parametrize(
    ("value", "fasting", "status", "action"),
    [
        (90, True, "in_range", "review_with_clinician"),
        (120, True, "above", "discuss_with_clinician"),
        (120, False, "in_range", "review_with_clinician"),
        (160, False, "above", "discuss_with_clinician"),
        (60, True, "below", "discuss_with_clinician"),
        (60, False, "below", "discuss_with_clinician"),
    ],
)
def test_reclassify_bands_by_timing(value, fasting, status, action):
    out = _reclassify_glucose(float(value), fasting=fasting)
    prov = out["provenance"]
    assert prov["status"] == status
    assert prov["metric"] == ("fasting_glucose" if fasting else "random_glucose")
    assert prov["carried_value"] == value
    assert out["action"] == action
    label = "fasting blood sugar" if fasting else "post-meal blood sugar"
    assert label in out["reply"]
    assert f"{value} mg/dL" in out["reply"]
    if status == "in_range":
        assert "within the typical range" in out["reply"]
    else:
        assert f"{status} the typical range" in out["reply"]
        assert "consult your doctor" in out["reply"].lower()


def test_reclassify_never_prints_a_float_artifact():
    assert "90 mg/dL" in _reclassify_glucose(90.0, fasting=True)["reply"]
    assert "92.5 mg/dL" in _reclassify_glucose(92.5, fasting=True)["reply"]


def test_param_tokens_fold_case_punctuation_and_plurals():
    assert param_tokens("Vitamin-D3 (25-OH)") == {"vitamin", "d3", "25", "oh"}
    assert param_tokens("Platelets") == {"platelet"}
    assert param_tokens("RBC") == {"rbc"}          # short words keep their s
    assert param_tokens("") == set()


def test_hemoglobin_aliases_exclude_the_corpuscular_indices():
    aliases = param_aliases(param_tokens("hemoglobin"))
    assert aliases is not None
    terms, exclude = aliases
    assert {"hemoglobin", "haemoglobin"} <= set(terms)
    assert {"mch", "mean corpuscular", "a1c"} <= set(exclude)

    def matches(series_name: str) -> bool:
        low = series_name.lower()
        return any(t in low for t in terms) and not any(x in low for x in exclude)

    assert matches("HEMOGLOBIN")
    assert matches("Haemoglobin")
    assert not matches("MEAN CORPUSCULAR HEMOGLOBIN(MCH)")
    assert not matches("Glycosylated Hemoglobin (HbA1c)")


def test_aliases_need_whole_term_equality_not_containment():
    # "hemoglobin" must not resolve to the HbA1c spec just because that spec's
    # terms mention hemoglobin.
    terms, _ = param_aliases(param_tokens("hemoglobin")) or ((), ())
    assert "hba1c" not in {t.lower() for t in terms}
    # The long tail the registry does not curate is left to token matching.
    assert param_aliases(param_tokens("basophils")) is None
    assert param_aliases(param_tokens("ggt")) is None

"""Phase 5 — mechanical claim grounding (pure)."""

from __future__ import annotations

from app.grounding.claims import (
    analyze_grounding,
    is_factual,
    strip_markers,
)


def test_is_factual():
    assert is_factual("HbA1c above 48 mmol/mol is high")
    assert is_factual("take 500 mg twice a day")
    assert is_factual("readings above 140 are a concern")
    assert not is_factual("a balanced diet supports wellbeing")


def test_strip_markers_cleans_spacing():
    text = "Blood sugar can run high [1]. Habits matter [P]."
    out = strip_markers(text)
    assert "[1]" not in out and "[P]" not in out
    assert out == "Blood sugar can run high. Habits matter."


def test_grounded_answer_with_valid_markers():
    answer = (
        "Type 2 diabetes relates to blood sugar. An HbA1c above 48 mmol/mol is "
        "worth discussing [2]. Everyday habits help [3]."
    )
    report = analyze_grounding(
        answer, num_chunks=3, has_patient_context=False, retrieval_happened=True
    )
    assert report.status == "grounded"
    assert report.factual_count == 1
    assert "2" in report.cited


def test_invalid_marker_flagged():
    answer = "An HbA1c above 48 mmol/mol is high [9]."
    report = analyze_grounding(
        answer, num_chunks=3, has_patient_context=False, retrieval_happened=True
    )
    assert report.status == "violations"
    assert any(v["type"] == "invalid_marker" for v in report.violations)


def test_ungrounded_factual_sentence_flagged():
    answer = "An HbA1c above 48 mmol/mol is high."
    report = analyze_grounding(
        answer, num_chunks=3, has_patient_context=False, retrieval_happened=True
    )
    assert any(v["type"] == "ungrounded_claim" for v in report.violations)


def test_gk_not_allowed_when_retrieval_happened():
    answer = "Blood pressure above 140 mmHg is often reviewed [GK]."
    report = analyze_grounding(
        answer, num_chunks=2, has_patient_context=False, retrieval_happened=True
    )
    assert any(v["type"] == "gk_not_allowed" for v in report.violations)


def test_gk_allowed_when_nothing_retrieved():
    answer = "In general, taking 500 mg twice a day would be a dosing example [GK]."
    report = analyze_grounding(
        answer, num_chunks=0, has_patient_context=False, retrieval_happened=False
    )
    assert report.status == "grounded"


def test_patient_marker_requires_patient_context():
    answer = "Your family history includes diabetes above baseline risk [P]."
    ok = analyze_grounding(
        answer, num_chunks=1, has_patient_context=True, retrieval_happened=True
    )
    # 'above baseline risk' has no number, so this sentence is non-factual; the
    # [P] marker is still valid because patient context is present.
    assert not any(v["type"] == "invalid_marker" for v in ok.violations)

    bad = analyze_grounding(
        answer, num_chunks=1, has_patient_context=False, retrieval_happened=True
    )
    assert any(v["type"] == "invalid_marker" for v in bad.violations)


def test_marker_after_terminator_is_normalized():
    answer = "An HbA1c above 48 mmol/mol is high. [1]"
    report = analyze_grounding(
        answer, num_chunks=2, has_patient_context=False, retrieval_happened=True
    )
    assert report.status == "grounded"


def test_non_factual_sentence_needs_no_marker():
    answer = "A balanced diet and regular activity support wellbeing."
    report = analyze_grounding(
        answer, num_chunks=2, has_patient_context=False, retrieval_happened=True
    )
    assert report.status == "grounded"
    assert report.factual_count == 0

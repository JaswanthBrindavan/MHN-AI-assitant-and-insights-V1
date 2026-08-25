"""The quality harness itself.

It cannot be run against a real model here, so what is tested is that the
SCORING is honest — in particular that it refuses to report a confident number
about something it did not measure. A quality suite that flatters the system is
worse than no quality suite, because Task 12 is gated on it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from scripts.quality_eval import CaseScore, Report, _overlaps, render, score_case

CASES = Path(__file__).resolve().parent.parent / "evals" / "quality_cases.json"


@dataclass
class _Result:
    response_message: str = ""
    provenance: dict = field(default_factory=dict)


def _long(text: str) -> str:
    """Pad past MIN_USEFUL_CHARS without changing the keywords."""
    return text + " " * 200


# --------------------------------------------------------------------------- #
# The case file
# --------------------------------------------------------------------------- #
def test_the_case_file_is_valid():
    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) >= 20
    names = [c["name"] for c in cases]
    assert len(names) == len(set(names)), "duplicate case names"
    for case in cases:
        assert case["message"].strip()


def test_the_cases_cover_both_education_and_the_readers_own_data():
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    assert any(c.get("expects_conditions") for c in cases)
    assert any(c.get("expects_tools") for c in cases)
    assert any(not c.get("expects_tools") for c in cases)


# --------------------------------------------------------------------------- #
# Scoring is honest about what it did not measure
# --------------------------------------------------------------------------- #
def test_tool_choice_is_unscored_against_a_fake():
    """A fake cannot decide to call a tool. Scoring it would report a confident
    zero — the most misleading number this file could produce."""
    score = score_case(
        {"name": "x", "message": "m", "expects_tools": ["get_documents"]},
        _Result(_long("a reply"), {}),
        model_chooses=False,
    )
    assert score.tool_ok is None


def test_tool_choice_is_scored_against_a_real_model():
    case = {"name": "x", "message": "m", "expects_tools": ["get_documents"]}
    hit = score_case(case, _Result(_long("a"), {"tools": ["get_documents"]}),
                     model_chooses=True)
    miss = score_case(case, _Result(_long("a"), {"tools": []}), model_chooses=True)
    assert hit.tool_ok is True
    assert miss.tool_ok is False


def test_engagement_is_unscored_when_no_model_and_no_script_wrote_the_reply():
    score = score_case(
        {"name": "x", "message": "what about my creatinine", "addresses": "creatinine"},
        _Result(_long("a generic default reply"), {}),
        model_chooses=False,
    )
    assert score.addressed is None


def test_engagement_is_scored_when_the_case_scripts_the_reply():
    case = {
        "name": "x", "message": "m", "addresses": "creatinine",
        "scripted": ["..."],
    }
    hit = score_case(case, _Result(_long("your creatinine was normal"), {}),
                     model_chooses=False)
    miss = score_case(case, _Result(_long("something unrelated entirely"), {}),
                      model_chooses=False)
    assert hit.addressed is True
    assert miss.addressed is False


def test_an_unscored_dimension_does_not_fail_a_case():
    """Unstated or unmeasurable expectations must not count against a case —
    otherwise the score punishes the harness's own limits."""
    score = CaseScore("x", answered=True, addressed=None, retrieval_ok=None,
                      tool_ok=None, degraded=None)
    assert score.passed


# --------------------------------------------------------------------------- #
# Answered / degraded
# --------------------------------------------------------------------------- #
def test_a_degraded_reply_is_not_an_answer():
    score = score_case(
        {"name": "x", "message": "m"},
        _Result(_long("safe fallback text"), {"degraded": "validation"}),
    )
    assert not score.answered
    assert not score.passed


def test_a_very_short_reply_is_not_an_answer():
    score = score_case({"name": "x", "message": "m"}, _Result("ok", {}))
    assert not score.answered


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #
def test_retrieval_scores_against_the_expected_conditions():
    case = {"name": "x", "message": "m", "expects_conditions": ["T2DM", "MC001"]}
    hit = score_case(case, _Result(_long("a"), {"conditions": ["MC001"]}))
    miss = score_case(case, _Result(_long("a"), {"conditions": ["MC099"]}))
    assert hit.retrieval_ok is True
    assert miss.retrieval_ok is False


# --------------------------------------------------------------------------- #
# The overlap heuristic
# --------------------------------------------------------------------------- #
def test_overlap_tolerates_inflection():
    """Exact matching was too literal: a reply about 'tiredness' scored zero
    against a question about feeling 'tired'."""
    assert _overlaps({"tired"}, {"tiredness"})
    assert _overlaps({"reports"}, {"report"})


def test_overlap_does_not_match_unrelated_words():
    assert not _overlaps({"creatinine"}, {"weather", "bicycle"})


def test_overlap_is_false_on_an_empty_reply():
    assert not _overlaps({"creatinine"}, set())


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def test_a_fake_run_says_what_it_did_not_measure():
    report = Report(engine="agentic", model_chooses=False,
                    scores=[CaseScore("x", True, True, None, None, None)])
    assert "UNSCORED" in render([report])


def test_a_real_run_does_not_carry_the_caveat():
    report = Report(engine="agentic", model_chooses=True,
                    scores=[CaseScore("x", True, True, None, True, None)])
    assert "UNSCORED" not in render([report])


def test_overall_counts_only_fully_passing_cases():
    report = Report(engine="e", scores=[
        CaseScore("a", True, True, True, True, None),
        CaseScore("b", True, True, True, False, None),
    ])
    assert report.overall == 50.0
    assert [s.name for s in report.failures()] == ["b"]


def test_an_empty_report_does_not_divide_by_zero():
    assert Report(engine="e").overall == 0.0

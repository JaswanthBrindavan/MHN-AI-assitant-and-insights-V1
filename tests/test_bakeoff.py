"""The bake-off harness itself.

It cannot be run against a real provider here (no key, no self-hosted model),
so what is tested is that the SCORING is honest: a provider that calls the
right tools must score higher than one that does not, and the metrics must not
pass vacuously.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.provider_bakeoff import CaseResult, ProviderReport, render

CASES = Path(__file__).resolve().parent.parent / "evals" / "bakeoff_cases.json"


def test_the_case_file_is_valid_and_covers_the_tools():
    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) >= 20

    names = [c["name"] for c in cases]
    assert len(names) == len(set(names)), "duplicate case names"
    for case in cases:
        assert case["message"].strip()

    # Every tool the model can call should be exercised by at least one case,
    # or the accuracy number is measuring less than it appears to.
    from app.chat.tools.definitions import TOOL_SPECS

    exercised = {t for c in cases for t in c.get("expects_tools", [])}
    for spec in TOOL_SPECS:
        assert spec.name in exercised, f"no bake-off case exercises {spec.name}"


def test_cases_without_expected_tools_are_scored_only_on_safety():
    """Those cases exist to catch OVER-refusal, so they must not drag the tool
    number around."""
    result = CaseResult(
        name="general", latency_s=0.1, degraded=None, expected_tools=[]
    )
    assert result.tool_ok


def test_calling_the_expected_tool_scores_and_missing_it_does_not():
    hit = CaseResult(
        name="a", latency_s=0.1, degraded=None,
        tools_called=["get_latest_metric"], expected_tools=["get_latest_metric"],
    )
    miss = CaseResult(
        name="b", latency_s=0.1, degraded=None,
        tools_called=[], expected_tools=["get_latest_metric"],
    )
    assert hit.tool_ok
    assert not miss.tool_ok


def test_extra_tool_calls_do_not_fail_a_case():
    """Calling something additional is untidy, not wrong — the model may have
    had a reason. Missing the required one IS wrong."""
    result = CaseResult(
        name="a", latency_s=0.1, degraded=None,
        tools_called=["get_latest_metric", "get_family_members"],
        expected_tools=["get_latest_metric"],
    )
    assert result.tool_ok


def test_a_better_provider_scores_higher():
    """The whole point: the table must actually separate them."""
    good = ProviderReport(label="good", results=[
        CaseResult("a", 0.1, None, ["get_latest_metric"], ["get_latest_metric"]),
        CaseResult("b", 0.1, None, ["lookup_medicine"], ["lookup_medicine"]),
    ])
    bad = ProviderReport(label="bad", results=[
        CaseResult("a", 2.0, "validation", [], ["get_latest_metric"]),
        CaseResult("b", 2.0, "fidelity", ["made_up_tool"], ["lookup_medicine"]),
    ])

    assert good.tool_accuracy == 100.0
    assert bad.tool_accuracy == 0.0
    assert good.safety_pass > bad.safety_pass
    assert good.fidelity_pass > bad.fidelity_pass
    assert good.degraded_rate < bad.degraded_rate
    assert good.latency(95) < bad.latency(95)


def test_cost_is_zero_without_a_known_rate():
    """Self-hosted has no token price; the GPU cost is recorded separately."""
    report = ProviderReport(label="local", results=[
        CaseResult("a", 0.1, None, usage={"input_tokens": 1000, "output_tokens": 500}),
    ])
    assert report.cost_per_turn(None) == 0.0
    assert report.cost_per_turn((1.0, 5.0)) > 0


def test_an_empty_report_does_not_divide_by_zero():
    empty = ProviderReport(label="none")
    assert empty.safety_pass == 0.0
    assert empty.latency(50) == 0.0
    assert empty.cost_per_turn((1.0, 5.0)) == 0.0


def test_the_table_renders_every_provider():
    reports = [
        ProviderReport(label="a:1", results=[CaseResult("x", 0.1, None)]),
        ProviderReport(label="b:2", results=[CaseResult("y", 0.2, None)]),
    ]
    table = render(reports, {})
    assert "a:1" in table and "b:2" in table
    assert "tools%" in table

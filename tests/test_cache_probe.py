"""The probe's job is honesty. These tests check it stays honest.

A measurement tool that reports a confident number about something it did not
measure is worse than no tool: it ends the investigation. The quality harness
had exactly this bug and reported agentic tool-choice at 0.0% against a fake
that cannot choose a tool.
"""

from __future__ import annotations

import pytest

from scripts import cache_probe


def test_no_credentials_means_no_hit_rate_is_printed():
    prefix = cache_probe.measure_prefix()
    report = cache_probe.render(prefix, "claude-sonnet-5", None, None)
    assert "HIT RATE: NOT MEASURED" in report
    assert "%" not in report, "a percentage implies a measurement that never happened"


def test_an_unmeasured_exact_count_says_so():
    prefix = cache_probe.measure_prefix()
    report = cache_probe.render(prefix, "claude-sonnet-5", None, None)
    assert "EXACT count: NOT MEASURED" in report
    assert "estimate is not a measurement" in report


@pytest.mark.parametrize(
    ("model", "minimum"),
    [
        # Verified against platform.claude.com prompt-caching docs, Aug 2026.
        ("claude-opus-5", 512),
        ("claude-fable-5", 512),
        ("claude-opus-4-8", 1024),
        ("claude-opus-4-7", 2048),
        ("claude-opus-4-6", 4096),
        ("claude-sonnet-5", 1024),
        ("claude-sonnet-4-6", 1024),
        ("claude-haiku-4-5", 4096),
        ("claude-haiku-3-5", 2048),
    ],
)
def test_the_minimum_is_per_model(model, minimum):
    """These span nearly an order of magnitude.

    A single constant was wrong, and wrong in the dangerous direction: the
    probe hard-coded 2048 for "any haiku", which is the RETIRED Haiku 3.5
    number. Haiku 4.5 needs 4096, so a 2,541-token prefix that the probe
    called borderline is in fact definitively too short there.
    """
    assert cache_probe._min_for(model) == minimum


def test_an_unknown_model_assumes_the_WORST_minimum():
    """Guessing low would report a prefix as cacheable when it is not.

    A new model id must fail loudly in the probe rather than quietly claiming
    a saving that is not being made.
    """
    from app.llm.anthropic import DEFAULT_MIN_CACHEABLE_TOKENS

    assert cache_probe._min_for("claude-model-that-does-not-exist-yet") == (
        DEFAULT_MIN_CACHEABLE_TOKENS
    )
    assert DEFAULT_MIN_CACHEABLE_TOKENS == max(
        m for _, m in __import__(
            "app.llm.anthropic", fromlist=["_MIN_CACHEABLE_BY_MODEL"]
        )._MIN_CACHEABLE_BY_MODEL
    )


def test_a_prefix_clearly_under_the_minimum_is_called_a_no_op_not_borderline():
    """"Clearly under" and "too close to call" are different answers.

    Only one of them is a reason to go and measure; calling a definitive
    failure "borderline" invites someone to assume it is probably fine.
    """
    prefix = cache_probe.measure_prefix()
    report = cache_probe.render(prefix, "claude-haiku-4-5", None, None)
    assert "NO-OP" in report
    assert "TOO CLOSE TO CALL" not in report


def test_a_prefix_near_the_line_says_go_and_measure():
    near = {
        "system_chars": 100,
        "tools_chars": 100,
        "system_tokens_estimated": 500,
        "tools_tokens_estimated": 500,
        "total_tokens_estimated": 1000,
    }
    report = cache_probe.render(near, "claude-sonnet-5", None, None)
    assert "TOO CLOSE TO CALL" in report


def test_a_prefix_near_the_minimum_is_flagged_not_waved_through():
    """The dangerous case is 'probably fine' — say the uncertainty out loud."""
    near = {
        "system_chars": 100,
        "tools_chars": 100,
        "system_tokens_estimated": 600,
        "tools_tokens_estimated": 600,
        "total_tokens_estimated": 1200,
    }
    report = cache_probe.render(near, "claude-sonnet-5", None, None)
    assert "TOO CLOSE TO CALL" in report


def test_an_exact_count_under_the_minimum_is_called_a_no_op():
    prefix = cache_probe.measure_prefix()
    report = cache_probe.render(prefix, "claude-sonnet-5", 400, None)
    assert "NO-OP" in report


def test_a_first_turn_that_wrote_nothing_is_a_warning():
    """Turn 0 writing nothing means the prefix never cached at all."""
    prefix = cache_probe.measure_prefix()
    live = {
        "observations": [
            {"turn": 0, "input_tokens": 500, "cache_creation": 0, "cache_read": 0},
            {"turn": 1, "input_tokens": 500, "cache_creation": 0, "cache_read": 0},
        ]
    }
    report = cache_probe.render(prefix, "claude-sonnet-5", 500, live)
    assert "WARNING" in report
    assert "FAILED" in report


def test_a_healthy_run_reports_the_hits_it_saw():
    prefix = cache_probe.measure_prefix()
    live = {
        "observations": [
            {"turn": 0, "input_tokens": 50, "cache_creation": 2400, "cache_read": 0},
            {"turn": 1, "input_tokens": 50, "cache_creation": 0, "cache_read": 2400},
            {"turn": 2, "input_tokens": 50, "cache_creation": 0, "cache_read": 2400},
        ]
    }
    report = cache_probe.render(prefix, "claude-sonnet-5", 2400, live)
    assert "HIT RATE (turns after the first): 2/2" in report
    assert "FAILED" not in report


def test_the_offline_run_exits_zero():
    """Reporting 'not measured' is this script SUCCEEDING.

    A non-zero exit would teach CI to ignore it, which is how an honest
    signal becomes noise.
    """
    assert cache_probe.main(["--measure"]) == 0


def test_the_measured_prefix_covers_the_tool_schemas():
    """Tools are the larger half of what is cached — omitting them would
    under-count by more than half and could wrongly call the prefix too short.
    """
    prefix = cache_probe.measure_prefix()
    assert prefix["tools_tokens_estimated"] > prefix["system_tokens_estimated"]
    assert (
        prefix["total_tokens_estimated"]
        == prefix["tools_tokens_estimated"] + prefix["system_tokens_estimated"]
    )


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-sonnet-5"])
def test_the_report_always_names_the_minimum_it_is_judging_against(model):
    prefix = cache_probe.measure_prefix()
    report = cache_probe.render(prefix, model, None, None)
    assert str(cache_probe._min_for(model)) in report

"""Metrics — correct exposition, and no way to leak PHI through a label.

The fiddly part of hand-rolling this is cumulative histogram buckets, so that
is tested directly rather than assumed.
"""

from __future__ import annotations

import pytest

from app import telemetry
from app.telemetry import (
    MAX_SERIES_PER_METRIC,
    Counter,
    Histogram,
    record_fail_open,
    render_prometheus,
    reset_all,
    timed,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_all()
    yield
    reset_all()


# --------------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------------- #
def test_a_counter_accumulates():
    c = Counter("t_total", "help")
    c.inc()
    c.inc(2)
    assert c.values[()] == 3


def test_labels_partition_a_counter():
    c = Counter("t_total", "help")
    c.inc(reason="validation")
    c.inc(reason="validation")
    c.inc(reason="fidelity")
    rendered = "\n".join(c.render())
    assert 't_total{reason="validation"} 2' in rendered
    assert 't_total{reason="fidelity"} 1' in rendered


def test_label_order_does_not_create_a_second_series():
    """Same labels, different keyword order, must be one series."""
    c = Counter("t_total", "help")
    c.inc(a="1", b="2")
    c.inc(b="2", a="1")
    assert len(c.values) == 1


# --------------------------------------------------------------------------- #
# Histograms — the part that is easy to get subtly wrong
# --------------------------------------------------------------------------- #
def test_buckets_are_cumulative():
    """Prometheus histogram buckets hold everything AT OR BELOW the bound, and
    +Inf equals the total. A non-cumulative histogram renders plausibly and
    computes wrong quantiles forever."""
    h = Histogram("t_seconds", "help", buckets=(1.0, 2.0, 3.0))
    for value in (0.5, 1.5, 2.5):
        h.observe(value)

    rendered = "\n".join(h.render())
    assert 't_seconds_bucket{le="1"} 1' in rendered
    assert 't_seconds_bucket{le="2"} 2' in rendered
    assert 't_seconds_bucket{le="3"} 3' in rendered
    assert 't_seconds_bucket{le="+Inf"} 3' in rendered


def test_a_value_above_every_bound_still_counts_in_inf_and_total():
    h = Histogram("t_seconds", "help", buckets=(1.0,))
    h.observe(99.0)
    rendered = "\n".join(h.render())
    assert 't_seconds_bucket{le="1"} 0' in rendered
    assert 't_seconds_bucket{le="+Inf"} 1' in rendered
    assert "t_seconds_count 1" in rendered


def test_sum_and_count_are_reported():
    h = Histogram("t_seconds", "help", buckets=(10.0,))
    h.observe(1.5)
    h.observe(2.5)
    rendered = "\n".join(h.render())
    assert "t_seconds_sum 4" in rendered
    assert "t_seconds_count 2" in rendered


def test_the_timer_records_even_when_the_block_raises():
    """Recording only successful turns would hide the slow failures, which are
    the ones worth seeing."""
    h = Histogram("t_seconds", "help")
    with pytest.raises(RuntimeError):
        with timed(h, engine="agentic"):
            raise RuntimeError("boom")
    assert h.totals[(("engine", "agentic"),)] == 1


# --------------------------------------------------------------------------- #
# Cardinality — the classic way metrics take down what they monitor
# --------------------------------------------------------------------------- #
def test_a_counter_stops_creating_series_at_the_cap():
    c = Counter("t_total", "help")
    for i in range(MAX_SERIES_PER_METRIC + 50):
        c.inc(unbounded=str(i))
    assert len(c.values) == MAX_SERIES_PER_METRIC


def test_a_histogram_stops_creating_series_at_the_cap():
    h = Histogram("t_seconds", "help")
    for i in range(MAX_SERIES_PER_METRIC + 50):
        h.observe(1.0, unbounded=str(i))
    assert len(h.counts) == MAX_SERIES_PER_METRIC


def test_existing_series_keep_counting_after_the_cap_is_hit():
    """The guard must stop NEW series, not stop counting."""
    c = Counter("t_total", "help")
    c.inc(kind="real")
    for i in range(MAX_SERIES_PER_METRIC + 10):
        c.inc(kind=f"junk{i}")
    c.inc(kind="real")
    assert c.values[(("kind", "real"),)] == 2


# --------------------------------------------------------------------------- #
# Exposition
# --------------------------------------------------------------------------- #
def test_the_registry_renders_help_and_type_for_every_metric():
    body = render_prometheus()
    for name in (
        "davi_chat_turns_total",
        "davi_chat_turn_seconds",
        "davi_degradations_total",
        "davi_fail_open_total",
        "davi_tool_calls_total",
        "davi_llm_tokens_total",
        "davi_document_reads_total",
    ):
        assert f"# HELP {name} " in body
        assert f"# TYPE {name} " in body


def test_quotes_and_newlines_in_a_label_cannot_break_the_format():
    c = Counter("t_total", "help")
    c.inc(reason='we"ird\nvalue')
    rendered = "\n".join(c.render())
    # One line, and the quote is escaped rather than terminating the label.
    assert len([ln for ln in rendered.splitlines() if ln.startswith("t_total")]) == 1
    assert '\\"' in rendered


def test_the_exposition_ends_with_a_newline():
    """Prometheus requires it; a missing trailing newline is a parse error."""
    assert render_prometheus().endswith("\n")


def test_reset_clears_everything():
    telemetry.degradations.inc(reason="validation")
    reset_all()
    assert telemetry.degradations.values == {}


def test_record_fail_open_counts_by_component():
    record_fail_open("grounding")
    record_fail_open("grounding")
    record_fail_open("tools")
    assert telemetry.fail_opens.values[(("component", "grounding"),)] == 2
    assert telemetry.fail_opens.values[(("component", "tools"),)] == 1

"""In-process metrics — pure stdlib, Prometheus text exposition.

The problem this solves is named in `project_docs/drawbacks.md` §4.3 and §8.1:
this service has six deliberate fail-open paths, each of which degrades to a
safe reply and logs a WARNING nobody reads. Without counters, the system can be
answering badly at scale and look perfectly healthy. "What fraction of replies
degraded last week, and why" has to be answerable.

**Why not prometheus-client.** Six metrics do not justify a dependency in a
codebase that holds patient data and keeps its supply chain deliberately short.
The text exposition format is a handful of lines per sample and is fully
specified; the fiddly part is cumulative histogram buckets, which is tested
here. If your ops stack expects the client library, swapping is mechanical —
see `project_docs/decisions-needed.md`.

**No PHI, ever.** Label values are drawn from bounded, code-defined sets. A
user id, a message, a condition name or a tool argument must never become a
label: metric cardinality is unbounded storage, and a label is not a log line
you can redact later.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

# Latency buckets in seconds. Chosen around what actually matters for a chat
# turn: sub-second feels instant, 1-3s is the working range for a tool round,
# and anything past 10s is a failure the reader experiences as one.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0,
)

# A hard ceiling on distinct label combinations per metric. Cardinality
# explosion is the classic way metrics take down the thing they monitor, and
# a bug that puts a user id in a label should fail loudly rather than quietly
# consume memory.
MAX_SERIES_PER_METRIC = 200

_lock = threading.Lock()


def _labels_key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _render_labels(key: tuple[tuple[str, str], ...]) -> str:
    if not key:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


@dataclass
class Counter:
    name: str
    help: str
    values: dict[tuple, float] = field(default_factory=dict)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = _labels_key(labels)
        with _lock:
            if key not in self.values and len(self.values) >= MAX_SERIES_PER_METRIC:
                return  # cardinality guard; see MAX_SERIES_PER_METRIC
            self.values[key] = self.values.get(key, 0.0) + amount

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        for key, value in sorted(self.values.items()):
            lines.append(f"{self.name}{_render_labels(key)} {value:g}")
        return lines


@dataclass
class Histogram:
    name: str
    help: str
    buckets: tuple[float, ...] = DEFAULT_BUCKETS
    counts: dict[tuple, list[int]] = field(default_factory=dict)
    sums: dict[tuple, float] = field(default_factory=dict)
    totals: dict[tuple, int] = field(default_factory=dict)

    def observe(self, value: float, **labels: str) -> None:
        key = _labels_key(labels)
        with _lock:
            if key not in self.counts:
                if len(self.counts) >= MAX_SERIES_PER_METRIC:
                    return
                self.counts[key] = [0] * len(self.buckets)
                self.sums[key] = 0.0
                self.totals[key] = 0
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    self.counts[key][i] += 1
            self.sums[key] += value
            self.totals[key] += 1

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for key, counts in sorted(self.counts.items()):
            # Prometheus histogram buckets are CUMULATIVE: each le= bucket
            # holds everything at or below it, and +Inf equals the total.
            running = 0
            for bound, count in zip(self.buckets, counts, strict=True):
                running = count
                labelled = _render_labels(key + (("le", _fmt(bound)),))
                lines.append(f"{self.name}_bucket{labelled} {running}")
            inf = _render_labels(key + (("le", "+Inf"),))
            lines.append(f"{self.name}_bucket{inf} {self.totals[key]}")
            lines.append(f"{self.name}_sum{_render_labels(key)} {self.sums[key]:g}")
            lines.append(f"{self.name}_count{_render_labels(key)} {self.totals[key]}")
        return lines


def _fmt(bound: float) -> str:
    return f"{bound:g}"


# --------------------------------------------------------------------------- #
# The metrics themselves
# --------------------------------------------------------------------------- #
# Every label value below comes from a bounded, code-defined set. Adding a
# label whose values come from user input is a bug — see the module docstring.

chat_turns = Counter(
    "davi_chat_turns_total",
    "Chat turns handled, by engine and risk level.",
)

chat_latency = Histogram(
    "davi_chat_turn_seconds",
    "Wall-clock seconds per chat turn, by engine.",
)

degradations = Counter(
    "davi_degradations_total",
    "Replies replaced by a safe fallback, BY REASON. The number that says "
    "whether the system is quietly answering badly.",
)

fail_opens = Counter(
    "davi_fail_open_total",
    "Guardrails that swallowed an exception and continued, by component. "
    "Each one is a WARNING nobody reads; this makes them countable.",
)

tool_calls = Counter(
    "davi_tool_calls_total",
    "Tool executions, by tool name and outcome.",
)

llm_tokens = Counter(
    "davi_llm_tokens_total",
    "Tokens consumed, by direction. Cost is this times the rate.",
)

document_reads = Counter(
    "davi_document_reads_total",
    "Document byte fetches, by outcome. 'refused' is the consent gate working.",
)

review_actions = Counter(
    "davi_review_actions_total",
    "Clinician review-queue actions, by action. Cross-user access to health "
    "information -- the one counter here that is about oversight, not health.",
)

feedback_received = Counter(
    "davi_feedback_total",
    "Reader verdicts on assistant turns, by rating and reason. The only "
    "signal here that comes from outside the system's own opinion of itself.",
)

_ALL = (
    chat_turns,
    chat_latency,
    degradations,
    fail_opens,
    tool_calls,
    llm_tokens,
    document_reads,
    feedback_received,
    review_actions,
)


def render_prometheus() -> str:
    """The whole registry in Prometheus text exposition format."""
    lines: list[str] = []
    for metric in _ALL:
        lines.extend(metric.render())
    return "\n".join(lines) + "\n"


def reset_all() -> None:
    """Clear every metric. For tests only."""
    with _lock:
        for metric in _ALL:
            if isinstance(metric, Counter):
                metric.values.clear()
            else:
                metric.counts.clear()
                metric.sums.clear()
                metric.totals.clear()


@contextmanager
def timed(histogram: Histogram, **labels: str) -> Iterator[None]:
    """Observe how long a block took, even when it raises.

    Recording only successful turns would hide the slow failures, which are
    the ones worth seeing.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        histogram.observe(time.perf_counter() - started, **labels)


def record_fail_open(component: str) -> None:
    """Count a swallowed exception.

    Call this from every ``except`` that continues rather than raising. The
    fail-open design is right; being unable to see it happening is not.
    """
    fail_opens.inc(component=component)

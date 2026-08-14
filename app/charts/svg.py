"""Tiny deterministic SVG chart renderer — pure stdlib, no dependencies.

Charts accompany chat replies as a ``visual`` payload: a declarative
``chart_spec`` (for clients that render natively) plus a self-contained
``svg`` string (for clients that just display it). No PHI beyond the plotted
numbers appears anywhere; rendering is fully deterministic.
"""

from __future__ import annotations

from html import escape

_W, _H = 520, 220
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 46, 14, 26, 34
_PLOT_W = _W - _PAD_L - _PAD_R
_PLOT_H = _H - _PAD_T - _PAD_B
_STROKE = "#2563eb"
_BAR = "#3b82f6"
_GRID = "#e5e7eb"
_TEXT = "#374151"


def _scale(values: list[float]) -> tuple[float, float]:
    lo, hi = min(values), max(values)
    if lo == hi:  # flat series — pad so the line sits mid-plot
        lo, hi = lo - 1, hi + 1
    span = hi - lo
    return lo - span * 0.08, hi + span * 0.08


def _fmt(v: float) -> str:
    return f"{v:g}"


def _frame(title: str, body: list[str]) -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_W} {_H}" '
        f'width="{_W}" height="{_H}" role="img">',
        f'<title>{escape(title)}</title>',
        f'<rect width="{_W}" height="{_H}" fill="white"/>',
        f'<text x="{_PAD_L}" y="17" font-family="sans-serif" font-size="13" '
        f'font-weight="bold" fill="{_TEXT}">{escape(title)}</text>',
        *body,
        "</svg>",
    ]
    return "".join(parts)


def _y_axis(lo: float, hi: float) -> list[str]:
    out: list[str] = []
    for i in range(5):
        frac = i / 4
        y = _PAD_T + _PLOT_H * (1 - frac)
        value = lo + (hi - lo) * frac
        out.append(
            f'<line x1="{_PAD_L}" y1="{y:.1f}" x2="{_W - _PAD_R}" y2="{y:.1f}" '
            f'stroke="{_GRID}" stroke-width="1"/>'
        )
        out.append(
            f'<text x="{_PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-family="sans-serif" font-size="10" fill="{_TEXT}">'
            f"{escape(_fmt(round(value, 1)))}</text>"
        )
    return out


def line_chart(title: str, labels: list[str], values: list[float]) -> str:
    """A single-series line chart. labels and values must be equal length ≥1."""
    if not values or len(labels) != len(values):
        raise ValueError("labels and values must be non-empty and equal length")
    lo, hi = _scale(values)
    body = _y_axis(lo, hi)

    n = len(values)
    step = _PLOT_W / max(n - 1, 1)
    points: list[str] = []
    for i, v in enumerate(values):
        x = _PAD_L + step * i
        y = _PAD_T + _PLOT_H * (1 - (v - lo) / (hi - lo))
        points.append(f"{x:.1f},{y:.1f}")
    body.append(
        f'<polyline fill="none" stroke="{_STROKE}" stroke-width="2" '
        f'points="{" ".join(points)}"/>'
    )
    for i, v in enumerate(values):
        x = _PAD_L + step * i
        y = _PAD_T + _PLOT_H * (1 - (v - lo) / (hi - lo))
        body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{_STROKE}"/>')

    shown = _label_indices(n)
    for i in shown:
        x = _PAD_L + step * i
        body.append(
            f'<text x="{x:.1f}" y="{_H - 12}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="10" fill="{_TEXT}">'
            f"{escape(labels[i][:12])}</text>"
        )
    return _frame(title, body)


def bar_chart(title: str, labels: list[str], values: list[float]) -> str:
    """A single-series bar chart (bars from zero)."""
    if not values or len(labels) != len(values):
        raise ValueError("labels and values must be non-empty and equal length")
    hi = max(max(values), 1.0) * 1.08
    lo = 0.0
    body = _y_axis(lo, hi)

    n = len(values)
    slot = _PLOT_W / n
    bar_w = min(slot * 0.6, 48.0)
    for i, v in enumerate(values):
        x = _PAD_L + slot * i + (slot - bar_w) / 2
        h = _PLOT_H * (v - lo) / (hi - lo)
        y = _PAD_T + _PLOT_H - h
        body.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
            f'fill="{_BAR}" rx="2"/>'
        )
        body.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{y - 4:.1f}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="10" fill="{_TEXT}">'
            f"{escape(_fmt(v))}</text>"
        )
    for i in _label_indices(n):
        x = _PAD_L + slot * i + slot / 2
        body.append(
            f'<text x="{x:.1f}" y="{_H - 12}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="10" fill="{_TEXT}">'
            f"{escape(labels[i][:12])}</text>"
        )
    return _frame(title, body)


def _label_indices(n: int, max_labels: int = 8) -> list[int]:
    if n <= max_labels:
        return list(range(n))
    step = (n - 1) / (max_labels - 1)
    return sorted({round(i * step) for i in range(max_labels)})


def chart_payload(
    kind: str, title: str, labels: list[str], values: list[float], unit: str | None = None
) -> dict:
    """The ``visual`` payload: declarative spec + rendered SVG."""
    svg = line_chart(title, labels, values) if kind == "line" else bar_chart(
        title, labels, values
    )
    return {
        "type": kind,
        "title": title,
        "unit": unit,
        "labels": labels,
        "values": values,
        "svg": svg,
    }

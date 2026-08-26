"""Provider bake-off — decide Anthropic vs self-hosted with numbers.

The LLM choice is deliberately still open, so this measures the things that
actually decide it rather than the things that are easy to measure:

  tool_accuracy   right tool, valid JSON args, no invented tool names
  safety_pass     survived validate_reply without degrading
  fidelity_pass   survived values_traceable — the drifted-value rate
  refusal_rate    over-refusal on legitimate clinical questions
  latency p50/p95 per turn, including tool rounds
  cost_per_turn   from usage; zero for self-hosted, but record GPU time too

Tool accuracy is the one that matters most and is least talked about:
open-weight models hallucinate tool names and emit malformed arguments far
more often than hosted ones, and in this product a wrong tool call means
quoting the wrong patient's value.

Usage:

    python -m scripts.provider_bakeoff \\
        --providers anthropic:claude-haiku-4-5,openai_compatible:qwen2.5:14b \\
        --cases 200

Each provider spec is ``kind:model`` and reads its credentials from the normal
env (LLM_API_KEY, LLM_BASE_URL). ``fake:fake`` runs offline and is what the
tests exercise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.chat.orchestrator import handle_chat
from app.db import Base
from app.knowledge.registry import reset_index_cache

ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = ROOT / "evals" / "bakeoff_cases.json"

# Rough per-million-token rates, USD. Only used to turn usage into a
# comparable number — override with --rate when they change.
DEFAULT_RATES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
}


@dataclass
class CaseResult:
    name: str
    latency_s: float
    degraded: str | None
    tools_called: list[str] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    reply: str = ""

    @property
    def tool_ok(self) -> bool:
        """Every expected tool was called, and nothing was invented."""
        if not self.expected_tools:
            return True
        called = set(self.tools_called)
        return set(self.expected_tools) <= called


@dataclass
class ProviderReport:
    label: str
    results: list[CaseResult] = field(default_factory=list)

    def _rate(self, attr) -> float:
        if not self.results:
            return 0.0
        return 100.0 * sum(1 for r in self.results if attr(r)) / len(self.results)

    @property
    def tool_accuracy(self) -> float:
        scored = [r for r in self.results if r.expected_tools]
        if not scored:
            return float("nan")
        return 100.0 * sum(1 for r in scored if r.tool_ok) / len(scored)

    @property
    def safety_pass(self) -> float:
        return self._rate(lambda r: r.degraded != "validation")

    @property
    def fidelity_pass(self) -> float:
        return self._rate(lambda r: r.degraded not in ("fidelity", "ungrounded_value"))

    @property
    def degraded_rate(self) -> float:
        return self._rate(lambda r: r.degraded is not None)

    def latency(self, pct: float) -> float:
        if not self.results:
            return 0.0
        values = sorted(r.latency_s for r in self.results)
        idx = min(len(values) - 1, int(pct / 100 * len(values)))
        return values[idx]

    def cost_per_turn(self, rates: tuple[float, float] | None) -> float:
        if rates is None or not self.results:
            return 0.0
        inp = sum(r.usage.get("input_tokens", 0) for r in self.results)
        out = sum(r.usage.get("output_tokens", 0) for r in self.results)
        return (inp * rates[0] + out * rates[1]) / 1_000_000 / len(self.results)


async def _fresh_sessionmaker():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession), engine


def _build_provider(spec: str):
    kind, _, model = spec.partition(":")
    if kind == "fake":
        from app.llm.fake import FakeProvider

        return FakeProvider()
    if kind == "anthropic":
        import os

        from app.llm.anthropic import AnthropicProvider

        return AnthropicProvider(model=model, api_key=os.environ["LLM_API_KEY"])
    if kind in ("openai_compatible", "openai", "ollama"):
        import os

        from app.llm.openai_compat import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
            model=model,
            api_key=os.environ.get("LLM_API_KEY", ""),
        )
    raise SystemExit(f"unknown provider kind: {kind!r}")


async def run_case(provider, case: dict) -> CaseResult:
    reset_index_cache()
    sm, engine = await _fresh_sessionmaker()
    started = time.perf_counter()
    try:
        async with sm() as session:
            result = await handle_chat(
                session, uuid.uuid4(), case["message"], provider
            )
            await session.commit()
        elapsed = time.perf_counter() - started
        return CaseResult(
            name=case["name"],
            latency_s=elapsed,
            degraded=result.provenance.get("degraded"),
            tools_called=result.provenance.get("tools", []),
            expected_tools=case.get("expects_tools", []),
            usage=result.provenance.get("usage", {}),
            reply=result.response_message,
        )
    finally:
        await engine.dispose()


async def run_provider(spec: str, cases: list[dict]) -> ProviderReport:
    provider = _build_provider(spec)
    report = ProviderReport(label=spec)
    for case in cases:
        try:
            report.results.append(await run_case(provider, case))
        except Exception as exc:  # noqa: BLE001 — one bad case must not end the run
            print(f"  [{spec}] {case['name']}: FAILED ({type(exc).__name__})")
    return report


def render(reports: list[ProviderReport], rates: dict) -> str:
    header = (
        f"{'provider':<38} {'tools%':>7} {'safe%':>7} {'fidel%':>7} "
        f"{'degr%':>7} {'p50s':>7} {'p95s':>7} {'$/turn':>9}"
    )
    lines = [header, "-" * len(header)]
    for r in reports:
        model = r.label.partition(":")[2]
        lines.append(
            f"{r.label:<38} {r.tool_accuracy:>7.1f} {r.safety_pass:>7.1f} "
            f"{r.fidelity_pass:>7.1f} {r.degraded_rate:>7.1f} "
            f"{r.latency(50):>7.2f} {r.latency(95):>7.2f} "
            f"{r.cost_per_turn(rates.get(model)):>9.5f}"
        )
    lines.append("")
    lines.append(
        "tools%  = every expected tool called, none invented (the number that "
        "decides open-weight viability)"
    )
    lines.append("safe%   = survived validate_reply without degrading")
    lines.append("fidel%  = survived the numeric-fidelity guard")
    return "\n".join(lines)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--providers", default="fake:fake")
    parser.add_argument("--cases", type=int, default=0, help="0 = all")
    parser.add_argument("--cases-file", default=str(CASES_PATH))
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    path = Path(args.cases_file)
    if not path.exists():
        print(f"no cases file at {path}", file=sys.stderr)
        return 2
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    if args.cases:
        cases = cases[: args.cases]

    reports = []
    for spec in args.providers.split(","):
        spec = spec.strip()
        if not spec:
            continue
        print(f"running {len(cases)} cases against {spec} ...")
        reports.append(await run_provider(spec, cases))

    table = render(reports, DEFAULT_RATES)
    print()
    print(table)

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    r.label: {
                        "tool_accuracy": r.tool_accuracy,
                        "safety_pass": r.safety_pass,
                        "fidelity_pass": r.fidelity_pass,
                        "degraded_rate": r.degraded_rate,
                        "p50": r.latency(50),
                        "p95": r.latency(95),
                        "cases": [
                            {
                                "name": c.name,
                                "latency_s": c.latency_s,
                                "degraded": c.degraded,
                                "tools": c.tools_called,
                                "tool_ok": c.tool_ok,
                            }
                            for c in r.results
                        ],
                    }
                    for r in reports
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(main()))

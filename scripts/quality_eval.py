"""Quality evals — does the assistant actually help?

`evals/scenarios.json` measures SAFETY: 15 invariants that must never break.
Nothing measured whether the thing is any *good*, which is why
`project_docs/drawbacks.md` §8.3 lists it as a gap, and why Task 12 (deleting
the regex engine) is gated on it: you cannot responsibly retire the engine
answering real users without evidence its replacement is at least as useful.

Four dimensions, each scored deterministically where possible:

  retrieval    did the right condition profile get retrieved?
  tool_choice  did the model reach for the tool the question needs?
  answered     did the reader get a real answer, or a fallback?
  addressed    does the reply actually engage with what was asked?

The first three are objective. The fourth is a keyword-overlap heuristic — a
crude proxy for helpfulness, and labelled as one. An LLM-judge would be better
and is the obvious upgrade, but a judge that needs a live model cannot gate CI,
and a rubric nobody has calibrated against human scores is not more trustworthy
for being expensive.

Run:
    python -m scripts.quality_eval --engine agentic
    python -m scripts.quality_eval --compare      # legacy vs agentic
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.chat.orchestrator import handle_chat
from app.db import Base
from app.knowledge.registry import reset_index_cache

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "quality_cases.json"

# A reply this short is a fallback or a deflection, not an answer.
MIN_USEFUL_CHARS = 80

_WORD = re.compile(r"[a-z]{4,}")
_STOP = frozenset(
    {
        "what", "when", "where", "which", "should", "would", "could", "about",
        "there", "their", "have", "this", "that", "with", "from", "your",
        "does", "doing", "been", "much", "many", "some", "just", "like",
        "know", "tell", "help", "please", "thing", "things",
    }
)


def _keywords(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}



def _overlaps(wanted: set[str], got: set[str]) -> bool:
    """Prefix-tolerant keyword overlap.

    Exact matching was too literal to be useful: a reply about "tiredness"
    scored zero against a question about feeling "tired". Prefix matching in
    either direction fixes the common inflection cases without pretending to
    understand meaning — this is still a crude proxy, and a reply that answers
    the question in entirely different words will be marked as missing it. The
    honest upgrade is an LLM judge; see the module docstring.
    """
    for w in wanted:
        for g in got:
            if w == g or w.startswith(g[:5]) or g.startswith(w[:5]):
                return True
    return False


@dataclass
class CaseScore:
    name: str
    answered: bool
    addressed: bool | None
    retrieval_ok: bool | None
    tool_ok: bool | None
    degraded: str | None
    reply: str = ""

    @property
    def passed(self) -> bool:
        """A case passes when it answered, engaged, and met every declared
        expectation. Unstated expectations do not count against it."""
        return (
            self.answered
            and self.addressed is not False
            and self.retrieval_ok is not False
            and self.tool_ok is not False
        )


@dataclass
class Report:
    engine: str
    scores: list[CaseScore] = field(default_factory=list)
    # False when a deterministic fake supplied the replies: tool choice and
    # answer quality are the MODEL's behaviour, and a fake has none.
    model_chooses: bool = True

    def _rate(self, pick) -> float:
        considered = [s for s in self.scores if pick(s) is not None]
        if not considered:
            return float("nan")
        return 100.0 * sum(1 for s in considered if pick(s)) / len(considered)

    @property
    def answered(self) -> float:
        return self._rate(lambda s: s.answered)

    @property
    def addressed(self) -> float:
        return self._rate(lambda s: s.addressed)

    @property
    def retrieval(self) -> float:
        return self._rate(lambda s: s.retrieval_ok)

    @property
    def tool_choice(self) -> float:
        return self._rate(lambda s: s.tool_ok)

    @property
    def overall(self) -> float:
        if not self.scores:
            return 0.0
        return 100.0 * sum(1 for s in self.scores if s.passed) / len(self.scores)

    def failures(self) -> list[CaseScore]:
        return [s for s in self.scores if not s.passed]


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
    return (
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
        engine,
    )


def score_case(case: dict, result, *, model_chooses: bool = True) -> CaseScore:
    """Score one answered case. Pure — so the scoring itself is testable."""
    reply = result.response_message or ""
    degraded = result.provenance.get("degraded")

    answered = bool(degraded is None and len(reply) >= MIN_USEFUL_CHARS)

    # Whether the reply engages is a property of the REPLY, so it is only
    # meaningful when a model wrote one — or when the case scripted the text
    # deliberately. Against a fake with no script, the reply is a generic
    # default and scoring it would report a confident zero about nothing.
    addressed: bool | None = None
    if model_chooses or case.get("scripted"):
        wanted = _keywords(case.get("addresses") or case["message"])
        addressed = _overlaps(wanted, _keywords(reply)) if wanted else True

    retrieval_ok: bool | None = None
    if case.get("expects_conditions"):
        got = set(result.provenance.get("conditions") or [])
        retrieval_ok = bool(set(case["expects_conditions"]) & got)

    tool_ok: bool | None = None
    if case.get("expects_tools") and model_chooses:
        # Left UNSCORED against a deterministic fake. A fake cannot decide to
        # call a tool, so scoring it would measure the harness and report a
        # confident zero — the most misleading number this file could produce.
        got_tools = set(result.provenance.get("tools") or [])
        tool_ok = set(case["expects_tools"]) <= got_tools

    return CaseScore(
        name=case["name"],
        answered=answered,
        addressed=addressed,
        retrieval_ok=retrieval_ok,
        tool_ok=tool_ok,
        degraded=degraded,
        reply=reply[:200],
    )



def _build_provider(spec: str, case: dict):
    """The provider for one case.

    "fake" replays the case's scripted reply — enough to score retrieval and
    whether the pipeline degraded, not enough to score tool choice.
    """
    if spec == "fake":
        from app.llm.fake import FakeProvider

        return FakeProvider(responses=case.get("scripted"))

    import os

    kind, _, model = spec.partition(":")
    if kind == "anthropic":
        from app.llm.anthropic import AnthropicProvider

        return AnthropicProvider(model=model, api_key=os.environ["LLM_API_KEY"])
    from app.llm.openai_compat import OpenAICompatibleProvider

    return OpenAICompatibleProvider(
        base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
        model=model,
        api_key=os.environ.get("LLM_API_KEY", ""),
    )


async def run_engine(
    engine_name: str, cases: list[dict], provider_spec: str = "fake"
) -> Report:
    import os

    os.environ["CHAT_ENGINE"] = engine_name
    from app.config import get_settings

    get_settings.cache_clear()

    model_chooses = provider_spec != "fake"
    report = Report(engine=engine_name, model_chooses=model_chooses)
    for case in cases:
        reset_index_cache()
        sm, engine = await _fresh_sessionmaker()
        try:
            async with sm() as session:
                provider = _build_provider(provider_spec, case)
                result = await handle_chat(
                    session, uuid.uuid4(), case["message"], provider
                )
                await session.commit()
            report.scores.append(
                score_case(case, result, model_chooses=model_chooses)
            )
        except Exception as exc:  # noqa: BLE001 — one bad case must not end the run
            print(f"  [{engine_name}] {case['name']}: ERROR {type(exc).__name__}")
            report.scores.append(
                CaseScore(case["name"], False, None, None, None, "error")
            )
        finally:
            await engine.dispose()
    return report


def render(reports: list[Report]) -> str:
    head = (
        f"{'engine':<12} {'overall':>8} {'answered':>9} {'addressed':>10} "
        f"{'retrieval':>10} {'tools':>8}"
    )
    lines = [head, "-" * len(head)]
    for r in reports:
        lines.append(
            f"{r.engine:<12} {r.overall:>7.1f}% {r.answered:>8.1f}% "
            f"{r.addressed:>9.1f}% {r.retrieval:>9.1f}% {r.tool_choice:>7.1f}%"
        )
    lines.append("")
    lines.append("answered  = a real reply, not a safety fallback")
    lines.append("addressed = the reply engages with what was asked (heuristic)")
    lines.append("retrieval = the expected condition profile was in scope")
    lines.append("tools     = the model reached for the tool the question needs")
    if reports and not reports[0].model_chooses:
        lines.append("")
        lines.append(
            "NOTE: run against a deterministic fake. Tool choice is "
            "UNSCORED (a fake cannot choose one), and 'addressed' is "
            "scored only where the case scripts a reply. What IS measured "
            "here: retrieval scope and whether the pipeline degraded. "
            "Pass --provider for a real quality number."
        )
    return "\n".join(lines)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="legacy")
    parser.add_argument(
        "--provider",
        default="fake",
        help=(
            "fake | anthropic:MODEL | openai_compatible:MODEL. A fake "
            "cannot choose tools, so tool choice is left unscored."
        ),
    )
    parser.add_argument("--compare", action="store_true",
                        help="run both engines and compare")
    parser.add_argument("--cases-file", default=str(CASES))
    parser.add_argument("--min-overall", type=float, default=0.0,
                        help="exit non-zero below this overall score (for CI)")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    path = Path(args.cases_file)
    if not path.exists():
        print(f"no cases at {path}", file=sys.stderr)
        return 2
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]

    engines = ["legacy", "agentic"] if args.compare else [args.engine]
    reports = []
    for name in engines:
        print(f"running {len(cases)} quality cases on {name} ...")
        reports.append(await run_engine(name, cases, args.provider))

    print()
    print(render(reports))

    for report in reports:
        failures = report.failures()
        if failures:
            print(f"\n{report.engine} — {len(failures)} case(s) below par:")
            for score in failures[:10]:
                why = []
                if not score.answered:
                    why.append(f"degraded={score.degraded or 'too short'}")
                if not score.addressed:
                    why.append("did not engage")
                if score.retrieval_ok is False:
                    why.append("wrong retrieval scope")
                if score.tool_ok is False:
                    why.append("wrong tool")
                print(f"  - {score.name}: {', '.join(why)}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    r.engine: {
                        "overall": r.overall,
                        "answered": r.answered,
                        "addressed": r.addressed,
                        "retrieval": r.retrieval,
                        "tool_choice": r.tool_choice,
                        "failures": [s.name for s in r.failures()],
                    }
                    for r in reports
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.out}")

    if args.compare and len(reports) == 2:
        legacy, agentic = reports
        if not agentic.model_chooses:
            print()
            print(
                "The Task 12 gate CANNOT be judged from a fake run: the "
                "difference between these engines IS which one lets the model "
                "choose. Re-run with --provider against a real model."
            )
            return 0
        print()
        if agentic.overall >= legacy.overall:
            print(
                f"AGENTIC >= LEGACY ({agentic.overall:.1f}% vs "
                f"{legacy.overall:.1f}%) — the Task 12 quality gate is met."
            )
        else:
            print(
                f"AGENTIC BELOW LEGACY ({agentic.overall:.1f}% vs "
                f"{legacy.overall:.1f}%) — do NOT retire the legacy engine."
            )

    worst = min((r.overall for r in reports), default=0.0)
    if args.min_overall and worst < args.min_overall:
        print(f"\nFAIL: {worst:.1f}% is below the {args.min_overall:.1f}% gate")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(main()))

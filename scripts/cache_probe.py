"""Measure whether prompt caching is actually working. Refuse to guess.

The plan's warning for this task, verbatim: "The minimum cacheable prefix is
~1024 tokens — if the stable prefix is shorter, it silently will not cache and
you will believe it is working."

That is the whole reason this script exists. A cache breakpoint that fails to
cache produces a byte-identical reply; nothing in the application can tell the
difference. Only `usage.cache_read_input_tokens` can, and only a live API call
returns it.

The plan said "~1024". The real minimum is PER MODEL and spans nearly an order
of magnitude -- 512 on Opus 5, 1024 on Sonnet 5, 4096 on Haiku 4.5. Our
~2,541-token prefix clears the first two and does NOT clear Haiku 4.5, where
the breakpoint caches nothing and reports no error. See
app/llm/anthropic.py::min_cacheable_tokens.

    python -m scripts.cache_probe --measure        # offline: prefix size only
    python -m scripts.cache_probe --model claude-haiku-4-5   # live: real hits

Without credentials it reports the prefix size and says plainly that the hit
rate was NOT measured. It never prints a hit rate it did not observe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from app.chat.tools.definitions import TOOL_SPECS
from app.llm.anthropic import min_cacheable_tokens
from app.rag.prompt import build_agentic_system_prompt, estimate_tokens

# How wrong the chars-per-token ratio could plausibly be, in either direction.
# Used to decide whether an estimate is close enough to the minimum that the
# honest answer is "go and measure" rather than a verdict.
ESTIMATE_ERROR = 0.25


def _min_for(model: str) -> int:
    """Delegates to the per-model table in app/llm/anthropic.py.

    This used to hard-code 2048 for "any haiku", which was the retired Haiku
    3.5 number. Haiku 4.5 needs 4096 -- so the probe would have called a
    2,541-token prefix "within the margin of error" when it is in fact
    definitively too short to cache on that model.
    """
    return min_cacheable_tokens(model)


def measure_prefix() -> dict:
    """Size the cacheable prefix WITHOUT a network call.

    Anthropic caches in the order tools → system → messages, so a breakpoint
    on the system block covers the tool schemas as well. Both are counted.
    """
    stable, _ = build_agentic_system_prompt("", None, None, None, True)
    tools_json = json.dumps(
        [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in TOOL_SPECS
        ]
    )
    return {
        "system_chars": len(stable),
        "tools_chars": len(tools_json),
        "system_tokens_estimated": estimate_tokens(stable),
        "tools_tokens_estimated": estimate_tokens(tools_json),
        "total_tokens_estimated": estimate_tokens(stable) + estimate_tokens(tools_json),
    }


async def count_exactly(model: str, api_key: str) -> int | None:
    """Exact token count via the API. None if the call fails.

    An estimate is enough to say "comfortably over" or "clearly under"; it is
    not enough to say "just over", which is exactly where a wrong answer
    costs the most.
    """
    from anthropic import AsyncAnthropic

    stable, _ = build_agentic_system_prompt("", None, None, None, True)
    client = AsyncAnthropic(api_key=api_key)
    try:
        result = await client.messages.count_tokens(
            model=model,
            system=[{"type": "text", "text": stable}],
            messages=[{"role": "user", "content": "hello"}],
            tools=[
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in TOOL_SPECS
            ],
        )
        return result.input_tokens
    except Exception as exc:  # noqa: BLE001 — a probe must report, not crash
        print(f"count_tokens failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


async def probe_live(model: str, api_key: str, turns: int) -> dict:
    """Send real turns and read the cache counters back.

    Turn 1 WRITES the cache (cache_creation_input_tokens > 0) and reads
    nothing — that is correct, not a failure. Turns 2+ must read.
    """
    from app.llm.anthropic import AnthropicProvider
    from app.llm.tools import UserMessage

    provider = AnthropicProvider(model=model, api_key=api_key, max_tokens=64)
    stable, volatile = build_agentic_system_prompt(
        "Patient context block for the probe.", None, None, None, True
    )

    observations = []
    for index in range(turns):
        # A DIFFERENT question each turn — the same question could be served
        # from somewhere else entirely and would prove nothing about the
        # prefix.
        turn = await provider.generate_turn(
            system=[stable, volatile + f"\n\nProbe turn {index}."],
            messages=[UserMessage(f"Say the number {index} and nothing else.")],
            tools=TOOL_SPECS,
        )
        usage = turn.usage or {}
        observations.append(
            {
                "turn": index,
                "input_tokens": usage.get("input_tokens"),
                "cache_creation": usage.get("cache_creation_input_tokens"),
                "cache_read": usage.get("cache_read_input_tokens"),
            }
        )
        print(f"  turn {index}: {observations[-1]}")

    return {"observations": observations}


def render(prefix: dict, model: str, exact: int | None, live: dict | None) -> str:
    minimum = _min_for(model)
    lines = ["", "PROMPT CACHE PROBE", "=" * 60, ""]
    lines.append(
        f"cacheable prefix (tools + system), estimated: "
        f"~{prefix['total_tokens_estimated']} tokens"
    )
    lines.append(
        f"  system rules : ~{prefix['system_tokens_estimated']} tokens "
        f"({prefix['system_chars']} chars)"
    )
    lines.append(
        f"  tool schemas : ~{prefix['tools_tokens_estimated']} tokens "
        f"({prefix['tools_chars']} chars)"
    )
    lines.append("")
    lines.append(f"minimum cacheable prefix for {model}: {minimum} tokens")

    if exact is not None:
        lines.append(f"EXACT count from the API: {exact} tokens")
        verdict = "OVER the minimum — caching can work" if exact >= minimum else (
            "UNDER the minimum — the breakpoint is a NO-OP"
        )
        lines.append(f"  -> {verdict}")
    else:
        lines.append(
            "EXACT count: NOT MEASURED (needs credentials). The estimate above "
            "is a characters-per-token ratio, not a tokenizer."
        )
        # Say which way the uncertainty cuts, rather than implying it is fine.
        # Three verdicts, not two: "clearly under" and "too close to call" are
        # different answers and only one of them warrants going and measuring.
        est = prefix["total_tokens_estimated"]
        if est * (1 + ESTIMATE_ERROR) < minimum:
            # Even if the ratio under-counts by ESTIMATE_ERROR, it does not
            # reach the minimum. This is a finding, not an uncertainty.
            lines.append(
                f"  -> UNDER the minimum by a wide margin. Even allowing "
                f"{ESTIMATE_ERROR:.0%} estimate error the prefix reaches only "
                f"~{est * (1 + ESTIMATE_ERROR):.0f} tokens. The breakpoint is a "
                f"NO-OP on {model}: it caches NOTHING and returns no error."
            )
        elif est * (1 - ESTIMATE_ERROR) < minimum:
            lines.append(
                "  -> TOO CLOSE TO CALL. Within the estimate's margin of error "
                "of the minimum. Do not assume caching works until this is "
                "measured against a real key."
            )
        else:
            lines.append(
                "  -> comfortably above the minimum on this estimate, but an "
                "estimate is not a measurement."
            )

    lines.append("")
    if live is None:
        lines.append("HIT RATE: NOT MEASURED.")
        lines.append(
            "  No API key, so no live turns were sent. A cache breakpoint that "
            "fails to cache is invisible from inside the application — the "
            "reply is identical and only usage.cache_read_input_tokens differs."
        )
        lines.append(
            "  Run with ANTHROPIC_API_KEY / LLM_API_KEY set to measure it."
        )
        return "\n".join(lines)

    obs = live["observations"]
    later = [o for o in obs if o["turn"] > 0]
    reads = [o for o in later if (o["cache_read"] or 0) > 0]
    lines.append(f"HIT RATE (turns after the first): {len(reads)}/{len(later)}")
    first = obs[0] if obs else {}
    if not (first.get("cache_creation") or 0):
        lines.append(
            "  WARNING: turn 0 wrote NOTHING to the cache. The prefix is "
            "almost certainly under the minimum, or the breakpoint is missing."
        )
    if later and not reads:
        lines.append(
            "  FAILED: the cache was never read. Either the prefix is too "
            "short, or something volatile leaked into it — check that the "
            "prefix is byte-identical across turns."
        )
    return "\n".join(lines)


async def main_async(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="claude-haiku-4-5")
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument(
        "--measure",
        action="store_true",
        help="offline only: size the prefix, send nothing",
    )
    args = parser.parse_args(argv)

    prefix = measure_prefix()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY")

    if args.measure or not api_key:
        if not api_key and not args.measure:
            print("No ANTHROPIC_API_KEY / LLM_API_KEY — offline measurement only.\n")
        print(render(prefix, args.model, None, None))
        # Exit 0: reporting honestly that it was not measured is a SUCCESS of
        # this script, not a failure. CI must not learn to ignore it.
        return 0

    exact = await count_exactly(args.model, api_key)
    print(f"sending {args.turns} live turns to {args.model} ...")
    live = await probe_live(args.model, api_key, args.turns)
    print(render(prefix, args.model, exact, live))

    later = [o for o in live["observations"] if o["turn"] > 0]
    reads = [o for o in later if (o["cache_read"] or 0) > 0]
    return 0 if later and len(reads) == len(later) else 1


def main(argv=None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    raise SystemExit(main())

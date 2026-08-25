# Task 23 — Prompt caching and context budget: what was built, and what was *not* measured

**Status:** implemented and unit-tested. **The cache hit rate has NOT been
measured** — that requires a live API key, which this environment does not
have. This document is the "written finding" the plan's acceptance criterion
allows in place of a measured number.

> **Plan, Task 23, verbatim:** *"assert `usage.cache_read_input_tokens > 0`
> across repeated turns. The minimum cacheable prefix is ~1024 tokens — **if
> the stable prefix is shorter, it silently will not cache and you will
> believe it is working.** Measure before claiming the win; if it is under the
> minimum, say so rather than shipping a no-op."*
>
> **Acceptance:** *"measured cache hit rate above 80% on multi-turn sessions,
> **or a written finding that the prefix is too short to cache**."*

---

## 1. The measurement

Run it yourself: `python -m scripts.cache_probe --measure`

| Component | Chars | Estimated tokens |
|---|---:|---:|
| Stable system rules (`build_agentic_system_prompt` prefix) | 2,972 | ~850 |
| Tool schemas (10 tools, JSON) | 5,917 | ~1,691 |
| **Total cacheable prefix** | **8,889** | **~2,541** |

Anthropic assembles the cacheable prompt in the order **tools → system →
messages**. A `cache_control` breakpoint on the system block therefore covers
the tool schemas as well. This matters a great deal here:

> **The system rules ALONE (~850 tokens) are UNDER the 1024-token minimum.**
> Had the breakpoint been placed on the system prompt without the tool
> schemas in front of it, caching would have silently done nothing — the
> precise failure the plan warned about. It is the tool schemas, the larger
> half, that carry the prefix over the line.

### Verdict per model

| Model family | Minimum prefix | Estimated prefix | Verdict |
|---|---:|---:|---|
| Opus / Sonnet | 1,024 | ~2,541 | Comfortably over — caching should work |
| **Haiku** | **2,048** | **~2,541** | **Within the margin of error. Do not assume.** |

The estimate uses a characters-per-token ratio (`CHARS_PER_TOKEN = 3.5`), not
a tokenizer. No local tokenizer is available: `anthropic` 1.0 exposes
`count_tokens` only as an API call, and `tiktoken` (a different tokenizer
anyway) is not installed. A ratio is enough to say "comfortably over" or
"clearly under". It is **not** enough to say "just over" — which is exactly
where Haiku sits, and exactly where a wrong answer costs the most.

**If Haiku is chosen as the production model, run the probe against a real key
before believing any cache saving.** `scripts/cache_probe.py` prints
`WITHIN THE MARGIN OF ERROR` for precisely this case rather than waving it
through.

---

## 2. What was built

### The breakpoint (`app/llm/anthropic.py`)

The `system` parameter now accepts `str | Sequence[str]`. A plain string
behaves exactly as before — no breakpoint, no change. A sequence means the
caller has split stable from volatile: **element 0 gets the breakpoint,
everything after it does not.**

Putting a breakpoint on the volatile half instead would be worse than none at
all: it would rewrite the cache on every single turn, paying the ~25%
cache-write premium forever and never once reading from it.

### The split (`app/chat/orchestrator.py`)

```
system = [stable, volatile + language_directive]
          ^^^^^^   ^^^^^^^^
          cached   changes every turn
```

The **language directive is deliberately in the volatile half.** It varies
with the reader's language, and a per-reader prefix caches for nobody.

### Prefix stability

The whole feature rests on the prefix being byte-identical. Tests enforce it:

- `test_the_prefix_is_byte_identical_across_wildly_different_turns` — a turn
  with patient context, compacted memory, recent turns and retrieved chunks
  produces the *same* prefix as an empty one.
- `test_patient_data_never_reaches_the_cached_prefix` — this one is a
  **safety** check, not a cost check. A cached prefix is reused across turns;
  PHI in it would be a leak surface. The test asserts a patient's name,
  values and conditions all stay in the volatile half.

One documented exception: `allow_questions` changes the prefix, because it
changes the *rules* rather than the data. It flips at most once per session
(when the clarifying-question budget runs out), costing one extra cache write
per session rather than one per turn.

### Per-turn directives (`app/chat/agent.py`)

Two paths append instructions mid-turn — the tool-budget-exhausted
`_FORCE_ANSWER` and the corrective `recover()` retry. Both went through
`system + directive`, which on a split prompt would have appended to the
**prefix**. `append_directive()` now puts them on the volatile tail.

This was a real bug and the suite caught it: `test_a_reply_that_stays_banned_
after_the_retry_falls_back` failed with `TypeError: can only concatenate list
(not "str") to list`. Had `system` been a string with a marker instead of a
list, it would have failed silently as a permanent cache miss.

### The context budget (`app/rag/prompt.py`)

The existing caps were **count**-based — top-k chunks, last 6 turns. That
bounds the *number* of items, not their size: one long retrieved chunk could
carry more text than the whole rest of the prompt.

`DEFAULT_VOLATILE_BUDGET_TOKENS = 6000` now bounds the bytes, and trimming
happens **before** rendering, not after. Truncating rendered text would cut a
chunk mid-sentence and hand the model a fact with its qualifier missing —
*"values above 7 are concerning **in untreated adults**"* cut short is worse
than no chunk at all.

Drop order, and why:

| Dropped | Rank | Reasoning |
|---|---|---|
| Retrieved chunks | first, lowest-ranked first | Costs one source the model could cite |
| Oldest conversation turns | second | Costs the thread of the conversation |
| The most recent turn | **never** | "Is that serious?" is meaningless without it |
| Patient context, compacted summary | **never** | Small, and it is the reader's own situation |

---

## 3. What is still unverified

| Claim | Status |
|---|---|
| Breakpoint is on the stable block only | **Verified** (unit test) |
| Prefix is byte-identical across turns | **Verified** (unit test) |
| No PHI in the cached prefix | **Verified** (unit test) |
| Split does not change what the model is told | **Verified** (unit test) |
| Per-turn directives don't disturb the prefix | **Verified** (unit test) |
| Budget trims chunks before turns | **Verified** (unit test) |
| Exact prefix token count | **NOT MEASURED** — needs a key |
| `cache_read_input_tokens > 0` on turn 2+ | **NOT MEASURED** — needs a key |
| Hit rate above 80% | **NOT MEASURED** — needs a key |

`scripts/cache_probe.py` performs the last three the moment a key exists:

```
ANTHROPIC_API_KEY=... python -m scripts.cache_probe --model claude-sonnet-5 --turns 3
```

It sends a *different* question each turn (the same question could be served
from elsewhere and would prove nothing about the prefix), and exits non-zero
if any turn after the first fails to read from the cache. Offline it exits
**zero** — reporting "not measured" is this script succeeding, and a non-zero
exit would teach CI to ignore it.

`tests/test_cache_probe.py` asserts the probe never prints a percentage it
did not observe.

---

## 4. Expected saving, stated as an expectation

At ~2,541 cached tokens per call, a cache read costs roughly 10% of a fresh
read while a cache write costs about 125%. For a session of *N* turns the
input-token bill for the prefix moves from `N × 2541` to roughly
`1.25 × 2541 + (N-1) × 0.1 × 2541` — around **85% off the prefix** by the
fifth turn.

**This is arithmetic from published rates, not a measurement.** It is not the
acceptance criterion, and it should not be quoted as a result. The
`davi_llm_tokens_total` counter will show the real number once a live
provider is in use.

---

## 5. Correction: where the ~2,541-token prefix does *not* apply

A review of this work caught an overclaim in §1 and in the original commit
message. Recording it here rather than quietly editing the number, because the
correction is more useful than the claim was.

**The ~2,541-token prefix only exists on calls that offer tools.** Anthropic
caches `tools → system → messages`; drop the tools and the request differs from
byte zero, so the cacheable prefix is the system rules **alone — ~850 tokens,
under the 1024 minimum.** Three call paths offer no tools:

| Call | Where | Tools offered | Effective prefix |
|---|---|---:|---|
| Normal agentic turn at NONE risk | `orchestrator.py` | `TOOL_SPECS` | ~2,541 — **cacheable** |
| Any turn at raised risk | `orchestrator.py` (`offered = TOOL_SPECS if risk == NONE else ()`) | none | ~850 — **not cacheable** |
| Forced answer after the tool budget | `agent.py` `run_agent` | none | ~850 — **not cacheable** |
| Corrective retry | `agent.py` `recover` | none | ~850 — **not cacheable** |

So `append_directive()` — written specifically for the last two — **cannot
preserve a cache hit on either path it was written for.** It is still correct
and still worth having: its job is to stop a per-turn directive being written
into the byte-identical prefix, which would poison caching for every
*subsequent* tools-bearing turn in the session. But it does not itself make
those two calls cache, and §2 should not have implied otherwise.

**What this means in practice.** Raised-risk turns and recovery retries are the
minority of traffic, and they are exactly the turns where latency matters least
and correctness matters most. The saving still lands where the volume is —
ordinary NONE-risk questions. But the honest statement is *"caching applies to
tools-bearing turns"*, not *"caching applies"*.

**What would change it.** Offering `TOOL_SPECS` on the forced-answer call while
setting `tool_choice: {"type": "none"}` would keep the prefix intact and still
force text. That is a real option, deliberately not taken here: `tool_choice` is
provider-specific, the OpenAI-compatible adapter spells it differently, and
buying a cache hit on a rare path by adding provider-specific branching to the
one module that is meant to be provider-neutral is a poor trade. Revisit it if
the metrics ever show these paths are not rare.

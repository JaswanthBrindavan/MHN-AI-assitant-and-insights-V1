# What Claude Sonnet 5 costs

The running cost record for the chosen production model. Re-run the numbers
with `python -m scripts.cache_probe --model claude-sonnet-5` once a key exists,
and replace the assumed usage with real figures the moment you have them.

**Every number here is DERIVED or ASSUMED. None of it is a measured bill.**

---

## Why Sonnet 5 and not Haiku 4.5

Haiku is a third of the price per token and would have cost more.

| Model | Input $/MTok | Min cacheable prefix | Our ~2,541-token prefix |
|---|---:|---:|---|
| Haiku 4.5 | $1.00 | **4,096** | **caches nothing, silently** |
| **Sonnet 5** | **$3.00** | **1,024** | **caches** |
| Opus 5 | $5.00 | 512 | caches |

Below the minimum, Anthropic declines to cache and **returns no error** — the
reply is byte-identical and only `usage.cache_read_input_tokens` differs. On
Haiku 4.5 our prefix would be a permanent no-op.

**At 10M users, caching is worth ~$1.97M/month.** A model that is 3× cheaper
per token but cannot cache is not cheaper.

---

## Per turn

Measured by rendering the real prompt builders:

| Block | Tokens | Cacheable |
|---|---:|---|
| Tool schemas | 1,691 | ✅ agentic |
| System rules | 850 | ✅ agentic |
| **Cached prefix (agentic)** | **2,541** | **clears 1,024** |
| Retrieved knowledge | 1,038 | ❌ |
| Conversation history | 698 | ❌ |
| Per-user memory (profile, episodes, recall, `[P]`) | 486 | ❌ |
| Compacted summary | 61 | ❌ |
| The question | 30 | ❌ |
| Reply (output) | ~257 | — |

**The legacy prefix is 267 tokens (560 with personalization rules) and does
NOT clear the 1,024 minimum.** Legacy offers no tools, so it has no schema
block to carry it. The breakpoint is in place and correct; it starts paying
only when that prefix grows. `CHAT_ENGINE` still defaults to `legacy`, so
today's traffic gets no caching at all.

### Per 6-turn session

| | Input tokens | Cost |
|---|---:|---:|
| Agentic, caching working | 18,325 | $0.055 |
| Agentic, if caching failed | 29,124 | $0.087 |
| Legacy (prefix too small) | 15,480 | $0.046 |

---

## Monthly

Assumed: 20% DAU, 6 turns per active day, 6 turns per session, 30.4 days.
Agentic with caching.

| Users | Turns/month | Input | Output | **Total** |
|---:|---:|---:|---:|---:|
| 10K | 364,800 | $3,342 | $1,406 | **$4,749** |
| 100K | 3.65M | $33,424 | $14,063 | **$47,487** |
| 1M | 36.5M | $334,243 | $140,630 | **$474,874** |
| 10M | 364.8M | $3,342,434 | $1,406,304 | **$4,748,738** |

Roughly **$0.47 per active user per month** at any scale — it is linear, so
the per-user figure is the number to argue about, not the total.

---

## The two levers

### 1. Caching — worth ~$1.97M/month at 10M

Already implemented on agentic. Two things would extend it:

- **Switch the default engine to agentic.** Legacy's prefix is too small to
  cache, so every legacy turn pays full price for its rules. Gated on Task 12.
- **A second breakpoint after a per-user memory block.** Breakpoints cache the
  cumulative prefix, so this would cache tools + rules + that reader's memory
  as one. Needs the memory block to be byte-stable, which is what
  `per-user-memory.md` §3.1 is for.

### 2. Per-user memory size — ~$532K/month at 10M

| | Monthly cost of +50 tokens |
|---|---:|
| 1M users | $5,472 |
| 10M users | $54,720 |

Per-user memory sits outside the cached prefix, so **every token is charged on
every turn, forever.** That reframes "what should Davi remember?" from a
storage question into a budgeting one — which is the right frame, and the
reason `per-user-memory.md` recommends bounding the block at ~900 tokens.

A field that seems free because it is 200 bytes on disk costs ~$55K/year at
1M users if it adds 50 tokens to every prompt.

---

## What is not measured

| | Status |
|---|---|
| Cache hit rate | **Not measured** — needs a key. `scripts/cache_probe.py` refuses to report a rate it did not observe. |
| Exact prefix token count | **Not measured** — the 2,541 is a chars/3.5 ratio, not a tokenizer. |
| Real DAU / turns per day | **Assumed.** Every figure scales linearly with these. |
| Tool-loop amplification | **Not modelled.** `run_agent` re-sends the system prompt each round (up to 3). At 1.2 average rounds the input side rises ~20%. |
| Output length | Assumed ~900 chars. |

Substitute real usage and the shape holds; only the magnitude moves.

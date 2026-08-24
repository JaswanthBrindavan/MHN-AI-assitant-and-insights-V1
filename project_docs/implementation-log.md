# Implementation Log

Process and architectural record of executing [`implementation-plan.md`](./implementation-plan.md).
**No code here** — code lives in git. This records *what was decided and why*.

Companions:
- [`findings.md`](./findings.md) — review findings per task
- [`decisions-needed.md`](./decisions-needed.md) — things awaiting your input
- [`handover.md`](./handover.md) — resume-from-here state
- [`memory.md`](./memory.md) — durable project context

**Method:** every task follows `.claude/execution-rules.md` — read the plan, inspect
the real files, find conflicts *before* coding, TDD, verify, then an independent
review pass under `.claude/review-rules.md`, fix findings, re-verify.

---

## Run summary

| Task | Title | Status | Commit |
|---|---|---|---|
| 1 | Internal tool-calling vocabulary | ✅ done | `91b6041`, `6ab2d92` |
| 2 | Anthropic adapter | ⏳ | |
| 3 | OpenAI-compatible adapter | ⏳ | |
| 4 | Numeric fidelity guard | ⏳ | |
| 5 | Tool definitions + executors | ⏳ | |
| 6 | Bounded agentic loop | ⏳ | |
| 7 | Wire agentic engine into orchestrator | ⏳ | |
| 8 | Clarifying questions | ⏳ | |
| 9 | SSE streaming | ⏳ | |
| 10 | Reply variation | ⏳ | |
| 11 | Latency: parallelize + cache | ⏳ | |
| 12 | Retire superseded parsers | 🔒 gated | |
| 13 | Provider bake-off harness | ⏳ | |
| 26 | Graceful recovery | ⏳ | |
| 27 | Non-numeric claim verification | ⏳ | |
| 28 | Postgres in CI | ⏳ | |
| 14 | User profile store | ⏳ | |
| 15 | Episode tracking | ⏳ | |
| 16 | Hybrid compaction | ⏳ | |

---

## Task 1 — Internal tool-calling vocabulary

**Goal.** Give the codebase one provider-neutral shape for tool calling, so
nothing above the adapter layer knows whether Anthropic or a self-hosted model
is live. This is the foundation the whole conversational engine sits on.

### Why this shape

The existing provider interface was one method — `generate(system, user) -> str`.
Text in, text out. That single signature is *why* the LLM was a leaf node in this
architecture: the interface had no vocabulary for anything else.

Tool calling needs a wider contract: offer tool schemas, receive structured call
requests, execute them, send results back, loop. Anthropic and OpenAI express
that with incompatible wire formats:

| | Anthropic | OpenAI-compatible |
|---|---|---|
| Declare | `tools[].input_schema` | `tools[].function.parameters` |
| Model asks | `stop_reason: "tool_use"` + `tool_use` block | `finish_reason: "tool_calls"` + `message.tool_calls[]` |
| Result back | ONE user message holding all `tool_result` blocks | one `tool`-role message **per** result |

Rather than let either vendor's shape leak upward, Task 1 defines an internal
vocabulary both adapters translate to and from.

### Decisions made

**1. `ToolResultMessage` holds a tuple of results, not a single result.**
Splitting parallel tool results across several messages silently teaches the
model to stop making parallel calls. Holding them in one message makes that
mistake *unrepresentable* rather than merely discouraged.

**2. `ToolCallingProvider` inherits `LLMProvider` rather than re-declaring it.**
The plan wrote out `model_name` and `generate` again. Inheriting means a
tool-calling provider is a strict superset, so nothing already annotated
`LLMProvider` needs re-annotating anywhere in the codebase.

**3. Pure stdlib, no dependencies.** `app/llm/tools.py` imports only
`dataclasses`. This matches the repo's existing purity invariant
(`insights/core.py`, `grounding/claims.py`, `chat/memory.py`) and means Task 1
adds nothing to the supply chain.

### Conflicts found before coding

An exhaustive pre-coding audit (10 agents, five parallel sweeps over every
`FakeProvider` construction site, attribute access, subclass, protocol consumer,
and failure-simulation pattern) found the plan contradicted the repo in four
ways. Details in [`findings.md`](./findings.md#task-1). The most important:

> **The plan would have made a safety eval vacuous.** It renamed
> `FakeProvider.DEFAULT` to `_DEFAULT_REPLY` with reworded text containing the
> word "clinician". But `evals/scenarios.json` proves the provider-outage path
> by asserting the reply *contains* "clinician" — a word supplied by the real
> degraded safe reply. New text would have made that scenario pass whether or
> not the provider ever failed.

Resolution: `DEFAULT` kept byte-for-byte, and the scenario was **proven
non-vacuous** by neutering the outage and confirming it drops to 14/15.

### Backward-compatibility decision

`FakeProvider.calls` changed from `list[tuple[str, str]]` to `list[dict]`.
Two options: a compat shim preserving both shapes, or updating the call sites.

**Chose: update the call sites.** The shim would have cost more lines than the
11 mechanical edits, and carrying dual-shape machinery forever to avoid touching
a few assertions is the kind of thing that reads fine today and is inexplicable
in six months. The edits also let three duplicate outage stubs be **deleted**
(`RaisingProvider`, `_RaisingProvider`, `ScriptThenRaiseProvider`), so the net
diff is *less* code than before.

### Unplanned work absorbed

Two problems surfaced that were not Task 1's scope but blocked it:

**Inherited breakage from `main`.** Verified against `origin/main` standalone:
3 ruff errors, 1 pyright error, and five `Path.read_text()` calls with no
`encoding=`. The last is severe — it raised `UnicodeDecodeError` under Windows
cp1252 on the em-dashes in `scenarios.json`, so `scripts/run_evals` and
`tests/test_evals.py` **could not run at all on Windows**. The safety eval gate
was silently unavailable.

**A message-ordering bug masquerading as flaky tests.** See the dedicated entry
below — it turned out to be the single most consequential thing found in this
task.

### Verification

| Check | Result |
|---|---|
| `pytest -p no:randomly` | 1269 passed, 0 failed (baseline: 4 failed / 1234 passed) |
| `pytest` × 2 random orders | 1269 passed both — flakiness eliminated |
| `ruff check .` | clean |
| `pyright` | 0 errors |
| `scripts/run_evals` | 15/15, and proven non-vacuous |
| mutation check | breaking `wants_tools` now fails a test (it did not before) |

---

## Cross-cutting fix — message ordering (found during Task 1)

**Not planned. Found because two tests looked "flaky".** They were not flaky.

`utcnow()` returned **one distinct value across 1000 consecutive calls** on this
machine — the system clock is coarser than a burst of inserts. Rows written in
the same tick therefore shared `created_at`, and `ORDER BY (created_at, id)`
fell back to `id`, which is a **random uuid4**.

`conversation_messages` is ordered that way in **six** places, including
`_ordered_messages`, which decides both:

- the recent turns the model is shown (`assemble_context`), and
- which messages compaction folds (`maybe_compact`).

The observed failure was `covers_through_message_id` pointing at the wrong
message: **compaction was folding the wrong turns.**

**Fix:** `utcnow()` is now strictly increasing per process, bumping one
microsecond on a tie. No schema change — insertion order becomes recoverable
from the timestamp alone, so nothing needs coordinating with Flyway.

**Why this mattered beyond the bug:** Task 7 feeds `assemble_context` output to
the model as conversational memory. Building an agentic engine on randomly
ordered history would have produced failures nearly impossible to diagnose —
the model would occasionally see turns out of order, with no error anywhere.

⚠️ This is a shared-helper change affecting every model's `created_at`. Logged
in [`decisions-needed.md`](./decisions-needed.md) for review.

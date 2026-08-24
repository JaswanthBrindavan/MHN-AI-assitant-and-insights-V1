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
| 2 | Anthropic adapter | ✅ done | `b89093f` |
| 3 | OpenAI-compatible adapter | ✅ done | `b89093f` |
| 4 | Numeric fidelity guard | ✅ done | `0df87e0` |
| 5 | Tool definitions + executors | ✅ done | `0df87e0` |
| 6 | Bounded agentic loop | ✅ done | `0df87e0`, `9b42df6` |
| 7 | Wire agentic engine into orchestrator | ✅ done | `f4389d9` |
| 8 | Clarifying questions | ✅ done | `48cfc50` |
| 9 | SSE streaming | ✅ done | `bd10503`, `a084efb` |
| 10 | Reply variation | ✅ done | `955fe91` |
| 11 | Turn efficiency (plan deviated) | ✅ done | `48cfc50` |
| 12 | Retire superseded parsers | 🔒 gated | |
| 13 | Provider bake-off harness | ✅ built (cannot run — no key) | `761748b` |
| 26 | Graceful recovery | ✅ done | `fe6703b` |
| 27 | Non-numeric claim verification | ✅ done | `fe6703b` |
| 28 | Postgres in CI | ✅ built (cannot run — no Docker) | `761748b` |
| 14 | User profile store | ✅ done | `7514b0a` |
| 15 | Episode tracking | ✅ done | `7514b0a` |
| 16 | Hybrid compaction | ✅ done | `801ca7c` |

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

---

## Tasks 2–3 — The two provider adapters

**Goal.** One internal vocabulary, two wire formats, so the deployment can move
between Anthropic and a self-hosted model without touching anything above the
adapter layer.

**Why two and not N.** The LLM choice is deliberately still open. Every
self-hosting stack worth considering — vLLM, Ollama, llama.cpp, LM Studio —
speaks the OpenAI `/chat/completions` format, so two adapters cover every
plausible future.

### Decisions

**`tools` is omitted, not sent empty, when none are offered.** The agent loop's
forced-final-answer call depends on that distinction: offering an empty list is
not the same as offering nothing, and the loop's only termination guarantee is
the second one.

**Date-suffixed model IDs are rejected at construction.** `railway.toml` had
`claude-haiku-4-5-20251001`; current IDs carry no date suffix. Failing at
construction means a stale ID breaks the deploy rather than 404-ing on the
first patient-facing request.

**Thinking is opt-in.** `{"type": "adaptive"}` requires a 4.6+ model; Haiku 4.5
returns 400. Sending it unconditionally would have broken the configured model.

**Parsing is deliberately forgiving on the OpenAI path.** Open-weight models
emit malformed tool arguments and invented finish reasons far more often than
hosted ones. A bad argument becomes an empty-argument call the executor
rejects; an unknown `finish_reason` degrades to `end_turn`, because a gateway
inventing a value must never look like a request to call tools.

---

## Tasks 4–6 — Fidelity guard, tools, agent loop

**The fidelity guard exists because of what the refactor changes.** Once tools
return raw data and the *model* composes the sentence, nothing else stops it
turning 6.1% into 6.5%. The banned-phrase validator has no opinion about
numbers, and grounding only checks that a marker is present, not that the
number behind it is right.

Only unit-bearing values are checked, so the guard has no opinion about ordinary
writing ("three things to discuss"). `digits_preserved` moved here from the
translation layer — same idea, pointed at the model instead of the translator.

**Tools return data AND the vetted wording.** Each executor returns structured
facts plus the handler's own validator-safe `deterministic_reply`. The model is
told to prefer that verbatim and to compose only when combining facts. This
keeps the reviewed phrasing as the default, and gives the fidelity guard
something to check against.

**The registry is fail-closed.** A tool never raises into the loop; each runs in
a SAVEPOINT so one failure cannot poison the session; arguments never reach the
log because they can carry PHI; and a hallucinated tool name is answered with
the list of real ones so the model stops guessing.

---

## Task 7 — The inversion

**This is the task the whole plan exists for.** The LLM can now reach the
reader's records through tools instead of sitting downstream of eleven regex
gates.

**Ordering is unchanged, and that is the point.** The engine branch sits at step
3.5 — after the triage floor, the scope guard, the emergency directive and the
canned conversational replies, all shared. The agentic engine therefore cannot
see an emergency. Six tests assert `provider.calls == []` on those paths: the
model is not merely overruled, it is never asked.

Tools are offered only at NONE risk. A red flag stays on the safe path so
nothing can delay or dilute an escalation.

**A gap the tests found.** With no tools called and nothing retrieved, `sources`
is empty and `values_traceable` has nothing to compare against — so an invented
dose passed. The agentic path had *weaker* numeric safety than legacy in that
case. Fixed as policy in the orchestrator (`ungrounded_value`): a dose stated
when nothing was retrieved and no tool ran has nothing behind it.

---

## Tasks 8–11 — Questions, variation, efficiency

**Clarifying questions need no state machine.** A clarifying question is a reply
that ends in a question mark. The only thing needing machinery is stopping a
loop, so the whole feature is one COUNT and one prompt rule. It fails closed: a
counting error suppresses questions rather than costing an answer. An emergency
is never met with a question — the floor returns before the model is asked.

**Variation stops where audit begins.** Greetings, identity replies, scope
declines and the NONE-risk safe reply come in sets, picked deterministically
from the session id. `EMERGENCY_DIRECTIVE` and `SELF_HARM_REPLY` are *not*
varied: that is audited clinical copy carrying the escalation directive the
validator requires and the Tele-MANAS number, and a paraphrased helpline
instruction is exactly the change that looks harmless and is not.

**Task 11 deviated from the plan, and the reason matters.** The plan called for
`asyncio.gather` over the independent per-turn lookups. That is impossible here:
they share one `AsyncSession`, and SQLAlchemy refuses concurrent operations on
one. The same mistake in the tool loop broke 3 of every 4 tool calls. So the win
came from doing *less* work — memoising patient context, removing a duplicate
`resolve_scope` call, and giving the registry cache a TTL.

---

## Tasks 26–27 — Recovery and non-numeric claims

**Recovery.** A guard rejection used to throw the answer away and substitute one
fixed sentence — a non-answer with no explanation, and two in a row read as a
broken bot. Each guard now gets ONE corrective retry naming what was wrong in
terms the model can act on. The rewrite must pass every guard again. Exactly one
retry, never a loop.

**Non-numeric claims.** `is_factual` only matched units and thresholds, so a
sentence with no digits was never grounding-checked at all — including "you can
stop taking it once you feel better", arguably the most dangerous sentence this
product could emit. Matched on grammatical *shape*, not a phrase blocklist,
because a blocklist is the same treadmill this work exists to end. Routing the
reader to care is never flagged.

Measured: 0 false positives on 4,000 real questions. That corpus is user
*questions*, not assistant answers, so treat it as a floor rather than proof.

---

## Task 9 — Streaming

You cannot verify a whole answer and stream it at once, so the compromise is
per-sentence: text accumulates and a sentence is released only once it passes
the banned-phrase check. Whole-answer guards run at the end and can **retract**
via a `replace` event — without that, a guard that only fires on the complete
answer would have no way to act on text already on the reader's screen.

Deterministic paths arrive as one delta. There is nothing to gain from typing
out an emergency directive one token at a time.

---

## Tasks 14–16 — Phase 2 memory

**Profile.** What the reader *told us*, distinct from `user_memories` (what they
*discussed*). Consent-gated, fail-closed on write. Deliberately not free text —
every field is a small enumerable fact, so what is held fits on one screen and
deletes in one call. A free-text notes column would be unauditable and would
quietly become a second, unreviewed medical record. The read and the erase
shipped in the same commit as the write.

**Episodes.** `ActiveSymptomState` had existed since the beginning with no
writer. Severity within an episode only ever rises, mirroring the floor's own
rule, and episodes are recorded at the severity the *floor* decided — never one
the model inferred.

**Hybrid compaction.** Prose alongside the structure, never instead of it. The
structured keys come from the same vocabulary as the safety floor, so if prose
could edit them a summarizer hallucination would become a safety decision. A
summarizer failure loses the prose and keeps the structure, never the reverse.

---

## Tasks 13, 28 — Built but not runnable here

Both are complete and tested as far as this machine allows; each needs one
thing it does not have. See [`decisions-needed.md`](./decisions-needed.md) D4.

- **13 (bake-off):** needs an API key or a self-hosted endpoint. The scoring is
  tested; running it offline gives 0% tool accuracy, correctly, because a
  deterministic fake never decides to call a tool.
- **28 (Postgres CI):** the workflow and dual-backend fixtures are written;
  Docker is unavailable here, so the `pg` job has not been observed green.

---

## What the review process actually caught

Worth recording, because it justifies the cost. Four defects that the whole test
suite passed straight through:

1. **`asyncio.gather` over tool calls broke 3 of every 4.** One `AsyncSession`
   cannot serve concurrent operations. The user-visible effect would have been
   the assistant claiming a reader's records were unavailable whenever the model
   asked for more than one thing at once — which the tool design encourages.
2. **`json.dumps` sat outside the failure boundary**, so a non-serialisable
   payload escaped the registry's never-raise contract entirely.
3. **The trace echoed the banned phrase** back to the client. The reply was
   correctly withheld while the trace quoted it verbatim.
4. **The plan would have made a safety eval vacuous** (Task 1, `DEFAULT` text).

### A process failure worth not repeating

Review agents were given write access to the tree they were reviewing. They left
nine scratch test files that monkeypatched global state without cleanup — with
them present, the **emergency-ordering tests failed**, which is the most
alarming possible signal from this suite and was pure artifact. One agent also
left `return None  # MUTANT` in `executors.py`, disabling lifestyle logging;
verified it never reached a commit.

**Reviewers must be read-only.** A reviewer that mutates the tree it is
reviewing can make working code look broken and broken code look fine.

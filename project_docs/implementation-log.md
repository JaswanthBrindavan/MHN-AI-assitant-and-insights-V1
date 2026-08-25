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
| 17 | Scoped document fetch | ✅ done | `5b6f4c7`, `335540c` |
| 18 | Vision | ✅ done | `5b6f4c7`, `d29cffe`, `335540c` |
| 19 | Voice | ✅ done | `5b6f4c7`, `335540c` |

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

---

## Phase 3 — Tasks 17, 18, 19 (vision and voice)

All three ship **off by default**. An empty base URL or `vision_enabled=false`
means the chat behaves exactly as before.

### Task 17 — the narrow exception to "no S3"

The security posture in `docs/production_integration.md` says Davi holds **no
AWS credentials**, and that is preserved rather than abandoned. Davi asks
*Spring* — which owns the bucket and already authorises file reads — to mint a
short-lived presigned GET, then reads those bytes in memory for one turn.

Three guards, in order:

1. **Davi checks consent itself**, with the same four-condition gate as every
   other family read, *before anything leaves the process*. A test asserts the
   stub client recorded zero calls for a refused document.
2. **Spring mints the URL** and re-checks while doing so. Both checks are
   deliberate: a bug in either alone cannot widen what the AI can read.
3. **Bounded and never persisted.** Streamed with an incremental size cap,
   content type rejected from the headers before any body arrives, nothing
   written to disk or to a table.

Every fetch writes a `job_runs` row, so *which documents the AI read* is
answerable from the database rather than from logs.

PDFs are deliberately excluded — mhn-ai already extracts those into
`content.ai`, and re-reading the raw file would duplicate its job with a worse
tool.

### Task 18 — vision, wired as a tool

Vision enters as a **tool** rather than an automatic step, for the same reason
every ability did in Phase 1: the model decides when it is relevant, and the
result lands in a tool result, which already flows through the validator, the
fidelity guard and the grounding verifier.

Two properties make it safe rather than reckless:

- **It inherits the consent gate.** `analyze_image` goes through
  `fetch_document_bytes`, so there is no path from a chat message to an image
  the family gate would deny.
- **Its output is untrusted text.** A vision model describing a photo is a
  generator like any other. A test asserts a vision reply claiming "you
  probably have dengue" fails the validator.

The prompts **refuse** rather than merely omit, because the failure mode is not
a model that says nothing — it is one that confidently identifies a rash, a
loose tablet, or a diagnosis from a photograph. The skin prompt forbids naming
a condition; the medicine prompt forbids identifying a loose tablet by colour
and shape; every prompt makes "I can't read that clearly" an acceptable answer,
or the model guesses instead.

`UserMessage` gained `attachments`, translated by both adapters. A data URI
rather than the presigned link — pointing a third party at that link would hand
out access this service was careful to keep scoped.

### Task 19 — voice, and the ordering rule

Transcription happens first and the transcript enters the **same** pipeline as
a typed message. There is no separate voice path, so a spoken red flag cannot
bypass the safety design by virtue of the input method.

Below HIGH, a low-confidence transcript is offered back for confirmation and
the pipeline is not run: "I can breathe" and "I can't breathe" differ by one
phoneme and by everything else.

---

## What the Phase 3 review caught

The review was **read-only this time**, after the Phase 1–2 round left scratch
files that made the emergency tests fail spuriously. It found a genuine safety
breach.

**The critical one was mine, and it was subtle.** The low-confidence voice
branch returned a reply *without running the triage floor*, hardcoding
`risk_level=NONE`. That is not "the floor did not run" — it is **lowering** it,
the one thing a floor forbids. A spoken "I can't breathe" at 0.22 confidence
got a chatty clarification question; a spoken "I want to kill myself" got one
too, with the Tele-MANAS helpline withheld.

The asymmetry ran exactly the wrong way. **ASR confidence collapses on
breathless, slurred, panicked or pained speech and in noisy places** — so the
gate fired hardest on precisely the people who most needed the escalation. And
the test written to cover that branch used *"I can breathe"*, the safe half of
the phoneme pair the module's own docstring names.

That is the shape of the mistake worth remembering: the code did what the
comment said, the test passed, and the design was still wrong.

Three more that the suite passed straight through:

- **Vision text was becoming an authorised numeric source.** An OCR misread
  (INR 1.0 read as 10.0) would have been *authorised* by the numeric-fidelity
  guard — the one guard that exists to catch exactly that.
- **`ensure_session` never checked ownership.** Passing another user's
  `session_id` loaded their history into your prompt. Pre-existing since Task
  16; Phase 3 added a third caller. Fixed once, in the shared function.
- **The family branch of the consent gate had zero test coverage** across 1559
  tests, because every test used `viewer == owner`, which short-circuits on the
  first line. The four-condition gate that is the entire reason that module is
  careful was never exercised.

And three tests that **could not fail**: the non-http URL test passed with the
guard removed, the transport test exercised the URL handler rather than the
byte handler its name claimed, and the vision test would have passed against an
implementation that dropped the image entirely.

---

# Phase 4 — Feedback, caching, oversight, and the drug refusal

Tasks 22–25. Where Phase 3 added capability, Phase 4 mostly added *ways to
find out we were wrong*: a loop from a bad reply to a regression test, a probe
that refuses to claim a cache saving it did not measure, a queue that puts
sensitive insights in front of a human, and two safety evals that turned up
two real holes in a path everyone assumed was closed.

## Task 22 — Feedback capture

`drawbacks.md` §8.2: nothing captured whether a reply was any good, so
improvement was entirely developer-initiated from anecdote.

The architectural decision that mattered was **what NOT to store**. A promoted
case does *not* carry the down-voted reply. That reply was the defect; freezing
it into `scripted` would enshrine the defect as the very thing the suite
protects. The promoter writes the question and a crude `addresses` seed, and a
human fills in what good looks like.

Two schema decisions, both about survival:

- **No FK from `turn_feedback` to `conversation_messages`.** Feedback must
  outlive the conversation it judges — otherwise clearing history erases the
  evidence behind a regression test.
- **Re-voting CORRECTS rather than duplicates** (unique on `user_id,
  message_id`). A reader who changes their mind should be able to, and counting
  both votes would skew the very numbers this exists to produce.

A found bug worth recording: the feedback counter was first declared in the API
module, where it would have rendered **nowhere**. `telemetry._ALL` is a
hand-maintained tuple, so any metric not added to it is invisible — a `/metrics`
page that silently omits a new metric looks exactly like one that includes it.
A test now fails if a metric is unregistered.

**Also shipped, unplanned:** `tests/test_flyway_parity.py`. CLAUDE.md says
production schema ships as Flyway while the test suite runs entirely against
Alembic-built schema. **Nothing compared the two.** A drifted column would pass
every test here and fail only in production, as a missing column at runtime.
V7 checked out clean; the guard is for the next one, and it refuses to let a
new `V*__davi_*.sql` be added without coverage.

## Task 23 — Prompt caching

The plan warned that a prefix under ~1024 tokens silently will not cache. That
warning turned out to be the crux, and measuring **first** is what caught it:

```
stable system rules : ~850 tokens   <-- UNDER the minimum on its own
tool schemas        : ~1691 tokens
total cacheable     : ~2541 tokens
```

Anthropic assembles the cacheable prompt as tools → system → messages, so a
breakpoint on the system block covers the tool schemas too. **It is the tools,
the larger half, that carry this prefix over the line.** A breakpoint on the
system prompt alone would have been a no-op indistinguishable from a working
one — same reply, only the usage numbers differ.

The design: `system` accepts `str | Sequence[str]`; element 0 is the
byte-identical prefix and carries the breakpoint. A plain string is untouched,
so no existing caller shifts behaviour, and every non-Anthropic provider joins
it straight back — **the split changes how tokens are billed, never what the
model is told.**

The language directive went into the *volatile* half deliberately: it varies
per reader, and a per-reader prefix caches for nobody.

One test here is a **safety** check rather than a cost check. A cached prefix is
reused across turns, so PHI in it would be a leak surface;
`test_patient_data_never_reaches_the_cached_prefix` asserts no name, value or
condition can land there.

A real bug the suite caught: two paths appended per-turn directives via
`system + directive` (tool-budget exhaustion, corrective retry). On a split
prompt both would have written to the **cached prefix**. It surfaced as a
`TypeError` — loudly, because the split is a list. Had the design used a marker
string instead, it would have failed *silently* as a permanent cache miss.

**The context budget** replaced count-based caps (top-k chunks, last 6 turns)
with a token budget. Counting items bounds the number of things, not their
size; one long chunk could carry more text than the rest of the prompt. Trimming
happens **before** rendering — truncating rendered text would cut a chunk
mid-sentence and drop the qualifier that made the fact safe. Chunks go before
turns (a dropped chunk costs a source; a dropped turn costs the thread), and
the latest turn and patient context are never dropped.

**What was NOT measured, and is said so plainly:** the exact token count and
the hit rate both need a live key. `scripts/cache_probe.py` measures them the
moment one exists and refuses to print a percentage it did not observe. On
Haiku the estimate sits *within the margin of error* of that model's 2048
minimum, and the probe says so rather than waving it through. Full finding:
`project_docs/task-23-caching.md`.

## Task 24 — Clinician review queue

`drawbacks.md` §8.7: `held_for_review` artifacts had been generated since
Phase 3 and **seen by nobody, ever**. The engine marked them sensitive, the
read endpoint filtered them out, and there it ended.

This is the only surface in the repo where one person reads **another
person's** health information, so three properties are enforced rather than
assumed:

1. **Membership is an explicit Davi-owned table.** There is no role claim in
   the production JWT (`sub` is a user UUID and nothing else) and we do not
   control what mhn-spring mints — so the roster lives here as rows an
   administrator adds deliberately. Revocation bites on the *next request*, not
   at token expiry.
2. **Every READ is audited, not just every decision** — by the time a decision
   exists, the information has already been seen. The audit write happens
   *before* the content is returned, so a crash in between is not an unlogged
   disclosure. A refused view writes nothing: nothing was disclosed.
3. **The queue listing carries no body.** Disclosing every held insight to
   anyone who opens the page would make the audited `view` step meaningless.

The subtle part was the **engine contract**. `"suppressed"` had to join
`LIVE_STATUSES`, because that tuple is what the hash-supersede check compares
against. Without it, every nightly sweep would create a fresh
`held_for_review` duplicate and the reviewer would decline the same insight
forever. Changed facts still produce a new held artifact — a suppression must
not become a permanent gag on a condition whose evidence later changes. Both
properties are tested against the **real engine**, not by asserting the
constant; removing `"suppressed"` fails those tests, not just a tautology.

Patients can query their own audit trail. "Who looked at my records?" is a
question they are entitled to have answered, and it is what makes the audit
mean something to the person it protects.

## Task 25 — Drug interactions

The plan called this a licensing task, not an engineering one, and said the
current refusal *"should not be softened"*. It was not softened. It was
**hardened**, because writing two safety evals for it exposed two real holes.

**Hole 1 — the refusal required a database hit.** If `drug_reference` did not
recognise either medicine, the question fell through to the LLM. Unrecognised
names are not exotic: foreign brands, misspellings, supplements, anything
outside the Indian dataset. People misspell medicine names most when they are
least sure what they are taking.

It now fires on the **phrasing**. The asymmetry settles it — a false refusal
costs a mildly unhelpful "ask a pharmacist"; a false *answer* about a real
interaction can hurt someone. Recognition is still computed but **recorded
rather than gated on**, because how often the refusal fires for unknown terms
is exactly the number that would justify buying a dataset. `NON_DRUG_TERMS`
grew everyday foods so "honey and lemon" still reaches the LLM.

**Hole 2 — the agentic engine never reached the refusal at all.** It dispatches
at step 3.5; the drug paths lived at step 5, inside the legacy chain. So under
`CHAT_ENGINE=agentic` the model answered interaction questions from its own
weights. **Retiring the legacy chain (Task 12) would have made that permanent
and invisible.** The refusal moved into the shared prologue beside the triage
floor, where a deterministic, provider-independent safety rule belongs.

This is the strongest argument yet for the Task 12 gate: the "shared prologue"
was assumed to hold everything safety-critical, and it did not. Every other
step-5 handler deserves the same audit before the legacy chain is retired.

**Also found:** `pytest-randomly` was missing from dev dependencies, so the CI
`ordering` job written in Task 28 **could never have run** — it would fail at
startup with "Error importing plugin randomly". A CI job that has never
executed is not a gate. Added; the suite is clean under three shuffles.

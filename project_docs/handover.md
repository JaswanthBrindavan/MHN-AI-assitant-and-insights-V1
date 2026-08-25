# Handover

> **If you are an agent resuming after a context compaction: read THIS file and
> [`implementation-log.md`](./implementation-log.md) before doing anything
> else, then [`memory.md`](./memory.md) for the invariants. Do not resume from
> a summary — several defects in this project came from acting on a
> plausible-but-wrong assumption about the repo, and these documents exist to
> prevent exactly that.**

**State:** Phases 0–4 of [`implementation-plan.md`](./implementation-plan.md)
are complete (Tasks 1–11, 13–28). **Task 12 remains deliberately blocked** —
see below, and note that Phase 4 found a second reason to keep it blocked.
**Branch:** `praveen-mhn`, merged with `origin/main`, **not pushed**.
**Verified:** 1729 passed · clean under three random seeds · ruff clean ·
pyright 0 · run_evals **17/17 on both engines** · coverage **91.3%**
(the gate was under-counting async code until Phase 4 — see below).

Nothing here changes behaviour for users yet: everything ships behind
`CHAT_ENGINE`, which still defaults to `legacy`. The one exception is the
drug-interaction refusal, which now fires more often and on **both** engines —
deliberately, see Phase 4 below.

---

## Read these first

| Document | What it holds |
|---|---|
| [`memory.md`](./memory.md) | Invariants and surprising facts. **Read before touching anything.** |
| [`implementation-log.md`](./implementation-log.md) | What was decided, and why (now through Phase 4) |
| [`findings.md`](./findings.md) | Review findings through Phase 3, including refuted ones |
| [`findings-phase-4.md`](./findings-phase-4.md) | Phase 4 findings + the mutation-check table |
| [`decisions-needed.md`](./decisions-needed.md) | **Choices made for you — please review** |
| [`task-23-caching.md`](./task-23-caching.md) | What was and was **not** measured about prompt caching |
| [`task-25-drug-interactions.md`](./task-25-drug-interactions.md) | The refusal change + the dataset decision that needs you |

---

## What Phase 4 added (Tasks 22–25)

| Task | What it is | Where |
|---|---|---|
| 22 | Feedback capture + one-command promotion of a bad reply into a regression case | `app/api/v1/feedback.py`, `scripts/promote_feedback.py` |
| 23 | Prompt-cache breakpoint on the stable prefix + a real token budget | `app/llm/anthropic.py`, `app/rag/prompt.py`, `scripts/cache_probe.py` |
| 24 | Clinician review queue for `held_for_review` insights, with an audit trail | `app/api/v1/review.py`, `app/models/review.py` |
| 25 | Interaction refusal hardened and moved into the shared prologue | `app/chat/orchestrator.py`, `app/drugs/service.py` |

New schema, shipped both ways as usual (Flyway for production, Alembic for
local/test): `V8__davi_feedback.sql`, `V9__davi_clinician_review.sql`.

**`tests/test_flyway_parity.py` is new and will fail if you add a
`V*__davi_*.sql` without registering its tables.** That is intentional — before
it existed, nothing compared the production DDL to the models.

---

## Phase 4 had a review round, and it found things

A read-only review agent went over all four Phase 4 commits after they landed.
Fourteen defects, all fixed in `a13f067`; the full list with severities is in
[`findings-phase-4.md`](./findings-phase-4.md). The three worth knowing about
before you touch anything:

1. **The coverage gate had been under-counting async code project-wide** —
   not a Phase 4 bug, a pre-existing one. SQLAlchemy async runs awaited DB
   calls inside a greenlet and coverage does not follow a greenlet switch, so
   everything after the first `await db.execute(...)` in a request handler
   read as unexecuted. `concurrency = ["thread", "greenlet"]` fixes it;
   the real total went 88.87% → 91.38%. **Any gap you thought was covered in
   async DB code before this should be re-checked.**

2. **Nothing tested that the orchestrator ships a split prompt** — the
   mechanism the whole caching feature rests on. Joining it back into one
   string kills caching and passed the entire suite. Two end-to-end tests now
   fail on that mutation. If you touch `orchestrator.py:~1104`, they are what
   protects you.

3. **I introduced a regression and the reviewer caught it.** Hardening the
   interaction gate made "Can I take my medicine with food?" produce
   "Whether medicine and food can be taken together depends on the doses…".
   Generic nouns are now exempt. The lesson is in D5.

---

## The three things that need you

### 1. The drug-interaction change (5 minutes) — the one real behaviour change

Read [`task-25-drug-interactions.md`](./task-25-drug-interactions.md). Short
version: the refusal used to require `drug_reference` to recognise a medicine,
so misspelled and unlisted names fell through to the LLM. It now fires on the
phrasing.

**Worked example of what changed:**

> "can I take rosuvastatin and clarithromycin together?"
> **before:** an LLM-composed answer · **now:** the deterministic
> check-with-a-pharmacist reply

> "can I take honey and lemon together?"
> **unchanged** — still the ordinary LLM path

One-line revert if you disagree, but I would push back: a false refusal costs a
mildly unhelpful reply; a false answer about a real interaction can hurt
someone.

### 2. The drug-interaction dataset (a purchasing decision, not a coding one)

Same document, §3. My recommendation is **keep the refusal for now** — a
half-covered interaction table returns "no interaction found", which readers
read as "no interaction exists", and that is a worse failure than an honest
refusal because the pipeline would be working correctly while producing it.

### 3. Review `decisions-needed.md` (10 minutes)

Unchanged from Phase 3, plus the Phase 4 entries at the end.

---

## Task 12 is still blocked, and Phase 4 strengthened the case

The gate was: run_evals ✅, a quality suite ✅ (built in Task 21), and one week
in staging ❌.

**Phase 4 added a fourth reason.** Two new safety evals found that the agentic
engine never reached the drug-interaction refusal at all — it dispatches at
step 3.5, and the drug paths sat at step 5 inside the legacy chain. Under
`CHAT_ENGINE=agentic` the model answered interaction questions from its own
weights.

That is fixed. But **the other ten step-5 handlers have not been audited for
the same problem.** The comment above the engine branch listed what was shared;
nobody had checked what *should* be. Before retiring ~1,200 lines of legacy
chain, walk every step-4 and step-5 handler and ask: *if this is deterministic
and safety-relevant, is it in the shared prologue or in the legacy branch?*

The quality suite cannot answer this for you. It runs against a fake provider,
which cannot choose a tool — `scripts/quality_eval.py --compare` refuses to
render a Task 12 verdict from a fake run, and it is right to.

---

## Still unverified, because it needs credentials or infrastructure

| Thing | How to verify when you can |
|---|---|
| Prompt-cache hit rate | `ANTHROPIC_API_KEY=… python -m scripts.cache_probe --model <model>` — exits non-zero if any turn after the first misses |
| Exact cacheable prefix size | same command; matters most on **Haiku**, where the estimate is within the margin of error of the 2048 minimum |
| Provider bake-off | `python -m scripts.provider_bakeoff` against a real provider |
| Real quality numbers | `python -m scripts.quality_eval --compare --provider anthropic:<model>` |
| Spring presigned GET | `GET /files/{resource_type}/{id}/url` — contract below |
| Vision model | needs `VISION_MODEL` + a provider |
| Voice sidecar | contract below |
| `pg` CI job | needs the GitHub workflow to actually run |


---

## Integration contracts still to be honoured

**Spring must expose** `GET /files/{resource_type}/{id}/url` returning
`{"url": "https://…"}` and honouring `Authorization: Bearer <MHN_SPRING_TOKEN>`
plus `X-User-Id`. See `docs/production_integration.md`.

**The voice sidecar must expose** `POST /transcribe` returning
`{text, language, confidence}` and `POST /speak` returning `{audio}` (base64).
A sidecar that does not report a confidence pins every transcript at 0.0, which
routes everything through the confirmation path — safe, but useless. Check that
before deploying.

**Synthesis is implemented but not wired** — `ChatResponse` has no audio field.
Wire it when a client can play audio back.

## One thing worth reading before touching the voice endpoint

The first version of it returned the low-confidence confirmation question with
`risk_level=NONE`, which meant a spoken "I can't breathe" at poor ASR
confidence got a chatty clarification instead of an emergency directive. ASR
confidence collapses on exactly the speech that signals an emergency —
breathless, panicked, pained — so the gate fired hardest on the people it most
needed to protect.

The floor now runs on the transcript unconditionally, and a red flag escalates
*before* the confirmation is appended. Do not reorder that.

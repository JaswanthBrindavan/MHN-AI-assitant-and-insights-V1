# Handover

> **If you are an agent resuming after a context compaction: read THIS file and
> [`implementation-log.md`](./implementation-log.md) before doing anything
> else, then [`memory.md`](./memory.md) for the invariants. Do not resume from
> a summary — several defects in this project came from acting on a
> plausible-but-wrong assumption about the repo, and these documents exist to
> prevent exactly that.**

**State:** Phases 0, 1, 2 and 3 of
[`implementation-plan.md`](./implementation-plan.md) are complete.
**Branch:** `praveen-mhn`, merged with `origin/main` (10 commits), **not pushed**.
**Verified:** 1583 passed · ruff clean · pyright 0 · run_evals 15/15 on both engines.

Nothing here changes behaviour for users yet: everything ships behind
`CHAT_ENGINE`, which still defaults to `legacy`.

---

## Read these first

| Document | What it holds |
|---|---|
| [`memory.md`](./memory.md) | Invariants and surprising facts. **Read before touching anything.** |
| [`implementation-log.md`](./implementation-log.md) | What was decided, and why |
| [`findings.md`](./findings.md) | Review findings, including refuted ones |
| [`decisions-needed.md`](./decisions-needed.md) | **Choices made for you — please review** |

---

## The four things that need you

### 1. Review `decisions-needed.md` (10 minutes)

Four autonomous calls, each with the reasoning and how to reverse it. The one
worth your attention is **D1**: `utcnow()` is now strictly increasing, which
affects every model's `created_at`. It fixed a real bug where compaction folded
the wrong messages, but it is a shared-helper change and the alternative (a
sequence column) needs a Flyway migration coordinated with mhn-spring.

### 2. Decide the LLM (blocks Task 12)

`scripts/provider_bakeoff.py` is built and its scoring is tested; it cannot run
here because there is no API key or self-hosted endpoint. One command once you
have either:

```bash
LLM_API_KEY=... CHAT_ENGINE=agentic python -m scripts.provider_bakeoff \
    --providers anthropic:claude-haiku-4-5,openai_compatible:qwen2.5:14b \
    --out evals/bakeoff.json
```

The number that decides it is **tool accuracy**. Open-weight models hallucinate
tool names and emit malformed arguments far more often than hosted ones, and
here a wrong tool call means quoting the wrong patient's value.

### 3. Run the agentic engine in staging

Set `CHAT_ENGINE=agentic`. Watch for `degraded` in `provenance` — the reasons
are `validation`, `fidelity`, `ungrounded_value`, `provider_error`. A week clean
unlocks Task 12.

### 4. Confirm the CI workflow

`.github/workflows/ci.yml` is written but has never executed — Docker is
unavailable on this machine, so the `pg` job is unobserved. It is the first
thing that will tell you whether the hybrid retrieval path actually works,
since `_hybrid_rank` short-circuits on SQLite and has therefore never run.

---

## Resume here

**Task 12 — retire the regex chain (~1,200 lines deleted).** Gated on:
1. `CHAT_ENGINE=agentic` passes run_evals — ✅ already true
2. Task 21's quality suite scores agentic ≥ legacy — ❌ not built
3. One week in staging with no regression — ❌ not run

Do not skip (3). The legacy engine is what currently answers real users.

**Then Phase 4 (measurement).** Task 20 (observability) and Task 21 (quality
evals) are what tell you whether any of this actually worked, and Task 21 gates
Task 12.

Phase 3 is done but three things need a live service before they can be
trusted: the Spring presigned-GET endpoint, a vision model, and the voice
sidecar. All three are wired, unit-tested and off by default.

---

## How to work on this

Follow `.claude/execution-rules.md`. The part that has repeatedly earned its
keep is **"do not blindly follow the plan if the repository contradicts it"** —
every task began with an audit of the real files against the plan, and every
audit found something.

Then `.claude/review-rules.md` for an independent pass. **Give reviewers
read-only access.** Agents with write access left scratch files that
monkeypatched global state; with them present the emergency-ordering tests
failed, which was pure artifact and the most alarming possible false signal.

### The gates

```bash
.venv/Scripts/python -m pytest -q -p no:randomly     # 1583 passed
.venv/Scripts/python -m pytest -q                    # random order, also clean
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m pyright
.venv/Scripts/python -m scripts.run_evals
CHAT_ENGINE=agentic .venv/Scripts/python -m scripts.run_evals
```

Both eval runs must be 15/15. If an agentic scenario fails, **fix the engine,
never the scenario.**

---

## Known gaps, stated plainly

**Deferred by decision:** WhatsApp (yours), proactive messaging (needs Task 15,
which now exists, plus a scheduler subsystem).

**Deferred deliberately:** the retrieval items (drawbacks §5.1, §5.4, §5.5).
Tool calling changes what retrieval is *for* — once the model calls
`get_condition_guidance` deliberately, the keyword-scoping hijacks that
motivated the stoplists matter far less. Planning them before Phase 1 landed
would have been planning the wrong fix. They deserve their own plan now.

**Not engineering:** every clinical constant still ships as **DRAFT — pending
clinician sign-off**. That is a release blocker no amount of code removes. The
new prompt rules, the recovery directives and the episode copy all need the same
review as the phrase tables.

**Unverified:** the `pg` CI job, the bake-off against a real provider, and the
Anthropic adapter against the live API. All three are one command away from
being verified; none is blocked on more code.

---

## If something looks broken

Check for stray `tests/test_zz_*.py` or `tests/test_tmp_*.py` first. Review
agents have left files that monkeypatch global state, and their symptom is
alarming and unrelated to whatever you just changed.

```bash
git status --short          # stray files?
git diff HEAD -- app/       # source touched unexpectedly?
grep -rn "MUTANT" app/      # a mutation left behind?
```

---

## Phase 3 additions

Three features, all **off by default**:

| Env | Effect |
|---|---|
| `MHN_SPRING_BASE_URL` + `MHN_SPRING_TOKEN` | Davi may read document bytes via a Spring-minted presigned GET. Davi still holds no AWS credentials. |
| `VISION_ENABLED` (+ optional `VISION_MODEL`) | The model may call `analyze_image` on a document the reader is entitled to. |
| `VOICE_BASE_URL` | `POST /api/v1/chat/voice` accepts a voice note. |

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

# Handover

**State:** Phase 0, Phase 1 and Phase 2 of
[`implementation-plan.md`](./implementation-plan.md) are complete.
**Branch:** `praveen-mhn`, merged with `origin/main` (10 commits), **not pushed**.
**Verified:** 1489 passed · ruff clean · pyright 0 · run_evals 15/15 on both engines.

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

**Then Phase 3 (multimodal) or Phase 4 (measurement).** Phase 4 first is the
better order: Task 20 (observability) and Task 21 (quality evals) are what tell
you whether Phase 1 actually worked, and Task 21 gates Task 12 anyway.

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
.venv/Scripts/python -m pytest -q -p no:randomly     # 1489 passed
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

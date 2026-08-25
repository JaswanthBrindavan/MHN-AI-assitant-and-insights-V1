# Decisions Needed

Choices made autonomously that you should review. Each one took the
**recommended** option and kept going, as instructed — nothing here is blocking.
If you disagree with any, say so and it will be changed.

Format: what was decided · why · what it looks like · how to reverse it.

---

## D1 — `utcnow()` is now strictly increasing (Task 1)

**Severity of the underlying bug: High. This one is worth reading first.**

### The problem

`app/models/common.py:utcnow()` returned `datetime.now(UTC)`. On this machine
**1000 consecutive calls returned one distinct value** — the system clock is
coarser than a burst of database inserts.

`conversation_messages` is ordered by `(created_at, id)` in six places. When
`created_at` ties, the tiebreak is `id` — a **random uuid4**. So message order
was random within any batch written in the same clock tick.

That is not cosmetic. `_ordered_messages` decides two things:

1. the recent turns the model is shown (`assemble_context`), and
2. which messages compaction folds (`maybe_compact`).

The observed failure was `covers_through_message_id` pointing at the wrong
message — **compaction folding the wrong turns**.

### What was decided

Make `utcnow()` strictly increasing per process: on a tie, bump one microsecond.

```python
# before — ties silently
def utcnow() -> datetime:
    return datetime.now(UTC)

# after — insertion order is recoverable from the timestamp alone
def utcnow() -> datetime:
    global _last_now
    with _clock_lock:
        now = datetime.now(UTC)
        if _last_now is not None and now <= _last_now:
            now = _last_now + timedelta(microseconds=1)
        _last_now = now
        return now
```

### Why this option

| Option | Verdict |
|---|---|
| **Monotonic `utcnow()`** ✅ | No schema change, fixes all six call sites at once, ~10 lines, fully testable |
| Add a sequence column | Correct, but **Flyway owns production schema** — needs a V11 migration coordinated with `mhn-spring`. Real cost, real lead time |
| Switch the PK to UUIDv7 | `uuid.uuid7()` is Python 3.14+; existing uuid4 rows would sort inconsistently against new ones |

### What you are trading

- **Every model's `created_at` is affected**, not just conversation messages.
  Timestamps become very slightly synthetic under burst writes.
- **Drift is bounded by write rate** — one microsecond per tied row. Reaching
  one second of drift needs a million writes in a tick.
- **Multi-process ties are still possible.** Two API workers writing in the same
  microsecond can still collide. The real case is covered: one turn's messages
  are written by one process in one transaction.

### If you disagree

Revert `app/models/common.py` and delete `tests/test_clock_monotonic.py`. Then
the correct fix is a monotonic sequence column on `conversation_messages`,
shipped as a new Flyway migration numbered above mhn-spring's head (V11 is
long since taken; check their chain first) and adopted into
mhn-spring — larger, slower, and strictly more correct.

---

## D2 — Update call sites rather than a compat shim (Task 1)

`FakeProvider.calls` changed shape from `list[tuple]` to `list[dict]`. Options
were a shim preserving both, or editing the callers.

**Decided: edit the callers** (11 mechanical edits), per your "whichever is
better in the end". The shim would have cost more lines than the edits, and the
edits let **three duplicate outage stubs be deleted** — so the change is
net-negative in total lines. Reversing means re-adding a shim, which is strictly
more code.

---

## D3 — Task 12 cannot be completed autonomously 🔒

Task 12 deletes the regex handler chain (~1,200 lines). Its own gate, from the
plan:

1. `CHAT_ENGINE=agentic` passes run_evals — *achievable*
2. Task 21's quality suite scores agentic ≥ legacy — *achievable*
3. **one week running in staging with no regression** — *not achievable overnight*

Deleting the deterministic engine without condition 3 would be reckless: it is
the fallback that currently answers real users. **Task 12 is left undone and
the flag stays `CHAT_ENGINE=legacy` by default.** Everything else in Phase 1
ships behind that flag, so nothing changes for users until you flip it.

**Your call:** run agentic in staging for a week, then Task 12 is a small,
mechanical deletion.

---

## D4 — Tasks that need credentials or infrastructure this machine lacks

Built and tested as far as possible; the final step needs something not
available here.

| Task | Built | Cannot do | Why |
|---|---|---|---|
| 13 — provider bake-off | The harness, tested against fakes | Run it | Needs an Anthropic API key and/or a self-hosted model endpoint |
| 28 — Postgres in CI | The CI workflow + dual-backend fixture | Verify it | CLAUDE.md notes Docker is unavailable on the dev machine; `_hybrid_rank` short-circuits on non-Postgres |
| 2 — Anthropic adapter | Adapter + tests against a mocked SDK | Live smoke test | No API key configured |

None block later tasks. Each is one command away once the credential or
container exists.

---

# Phase 4 decisions (Tasks 22–25)

## D5 — The drug-interaction refusal now fires without a database match

**What changed.** "Can I take X with Y" used to reach the deterministic
check-with-a-pharmacist reply only when `drug_reference` recognised at least one
of the two terms. It now fires on the phrasing alone.

**Why.** The old gate sent unrecognised names to the LLM. Unrecognised does not
mean obscure — it means foreign brands, generic names absent from an
Indian brand dataset, supplements, and above all **misspellings**, which are
most likely precisely when the reader is unsure what they are holding.

**The trade, plainly:**

| | Cost |
|---|---|
| False refusal | a mildly unhelpful *"ask a pharmacist about that combination"* |
| False answer | someone combines two medicines that should not be combined |

**Example — changed:**

> **You:** can I take rosuvastatin and clarithromycin together?
> **Before:** an LLM-composed answer with a `[GK]` marker
> **Now:** "Whether rosuvastatin and clarithromycin can be taken together
> depends on things I cannot verify from here — the doses, the timing, your
> other medicines… please ask a pharmacist or the prescriber."

**Example — unchanged:**

> **You:** can I take honey and lemon together?
> **Still** the ordinary LLM path. `NON_DRUG_TERMS` grew a list of everyday
> foods (lemon, ginger, turmeric, curd, banana, dal, roti…) so food pairings
> are not caught by the tighter gate.

**To reverse:** restore the `if matched_any:` condition in
`_interaction_refusal` (`app/chat/orchestrator.py`). One line. I would push
back on it, for the asymmetry above.

---

## D6 — The interaction refusal moved into the shared prologue

**What changed.** It runs before engine selection, so `CHAT_ENGINE=agentic`
gets it too.

**Why this is not really a choice.** Before the move, the agentic engine
answered interaction questions from the model's own weights — it dispatches at
step 3.5 and the drug paths sat at step 5. Two new safety evals caught it
(agentic scored 15/17 while legacy scored 17/17).

**What you should take from it:** the other ten step-4/step-5 handlers have not
been audited for the same problem. That audit is now a prerequisite for Task 12
and is recorded in the handover.

**To reverse:** there is no sensible reverse. A deterministic,
provider-independent safety rule belongs above the engine branch.

---

## D7 — The drug-interaction dataset: my recommendation is to buy nothing yet

This is the one genuinely open question in Phase 4, and it is a purchasing
decision rather than an engineering one. Full options table in
[`task-25-drug-interactions.md`](./task-25-drug-interactions.md) §3.

**Recommendation: keep the refusal.** Three reasons:

1. The refusal is already the right answer to most real questions. Whether two
   medicines can be combined depends on dose, timing, renal and hepatic
   function and the rest of the patient's list — a pairwise table answers a
   narrower question than the one people ask.
2. **A half-covered dataset is worse than none.** At 60% coverage the other 40%
   return "no interaction found", which reads as "no interaction exists". That
   failure is more dangerous than today's, because the pipeline would be
   working correctly while producing it.
3. The same money buys more elsewhere — corpus coverage, or clinician review
   time.

**If you decide to buy:** DrugBank first. The engineering after that is about a
day, and the "no data for this pair" branch must return today's refusal text
**verbatim**, so absence never reads as safety.

---

## D8 — `"suppressed"` became a live artifact status

**What changed.** `LIVE_STATUSES` in `app/insights/engine.py` gained
`"suppressed"`.

**Why.** That tuple is what the hash-supersede check compares against. Without
it, a clinician's decision to withhold an insight would be undone by the next
nightly sweep, which would create a fresh `held_for_review` duplicate — the same
decision demanded of the same reviewer, forever.

**The counterpart:** changed facts still produce a new held artifact. A
suppression must not become a permanent gag on a condition whose evidence later
changes.

**Not reversible without breaking the queue.** Both properties are pinned by
behavioural tests against the real engine
(`tests/test_review_recompute.py`).

---

## D9 — Clinician membership is a database table, not a token claim

**What changed.** A row in `clinician_reviewers` is what grants cross-user read
access to sensitive insights.

**Why not a JWT role claim.** Production session JWTs carry `sub` (a user UUID)
and nothing else, and mhn-spring mints them — we do not control their contents.
Waiting for a role claim would have meant either shipping nothing or inventing
a claim Spring does not issue.

**What this costs you.** Granting review access is a deliberate `INSERT` by an
administrator. There is no self-service path and no UI. That is intentional for
a first version: this is the only surface in the product where one person reads
another person's health information.

**Revocation** is `active = false`, which takes effect on the next request. Do
**not** delete the row — the audit trail references it, and the grant's history
is part of the record.

---

## D10 — `pytest-randomly` added to dev dependencies

**What changed.** One line in `pyproject.toml`.

**Why.** The CI `ordering` job written in Task 28 runs
`pytest -p randomly --randomly-seed=1`. The plugin was never a dependency, so
that job would have failed at startup with `Error importing plugin "randomly"`.
A CI job that has never executed is a green checkmark for nothing.

**Note:** installing it changes the *default* local test order to shuffled. Every
command in CI and in the docs passes `-p no:randomly` where deterministic order
is wanted. The suite is verified clean under seeds 1, 2 and 12345.

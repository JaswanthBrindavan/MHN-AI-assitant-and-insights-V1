# Open items — decisions, blocked work, and unfixed findings

> **Updated 2026-08-25 — branch `praveen-mhn`, PR open.**
>
> **The schema is live.** `V21__davi_chat_platform.sql` is merged into
> mhn-spring's main and its tables are present in the staging database. Nothing
> here is waiting on a migration any more.
>
> **Closed:** A1 (Sonnet 5), A5 (memory document BUILT), A6 (emergency
> recording), A8 (job_runs actor), A9/D1 (monotonic clock), C1 (complete
> deferred erasure), C2 (session-list scan), C3 (retention), plus period data
> and adherence.
>
> **Found and fixed since:** mhn-spring's V18 silently made three
> patient-facing answers wrong; a drug reply ignored the reader's allergies;
> Davi's V7–V10 collided with mhn-spring's Flyway chain.
>
> **Still open:** A2 (Task 12 — staging), A3, A4, A7, C4, C5, **C6**, C7–C11,
> and
> everything in section B. See [`handover.md`](./handover.md) for the ordered
> pick-up list.

Everything outstanding, in one place. Three sections: **what needs your
decision**, **what is blocked on something I do not have**, and **findings that
are real and not yet fixed**.

Each item says how it was established — **measured**, **verified** (read in the
source), or **derived**. Nothing here is a guess presented as a fact.

---

## A. Waiting on your decision

### A1 — Which production model? ✅ DECIDED: Sonnet 5
`railway.toml:44` documents `claude-haiku-4-5`. **Verified:** its minimum
cacheable prefix is 4,096 tokens; ours measures ~2,541. On Haiku 4.5 prompt
caching does nothing at all and returns no error.

| Model | Min prefix | Our ~2,541 prefix | Input $/MTok |
|---|---:|---|---:|
| Opus 5 | 512 | caches | $5.00 |
| Sonnet 5 | 1,024 | caches | $3.00 |
| **Haiku 4.5** | **4,096** | **caches nothing** | $1.00 |

This decides whether any caching work pays at all, and it gates the prompt-cache
rung in `per-user-memory.md`. **Nothing else in the backlog is worth doing
before this.** Verify with `python -m scripts.cache_probe --model <model>`.

### A2 — Task 12: run agentic in staging for a week?
The only unshipped plan task. Gate: run_evals ✅ (17/17), quality ≥ legacy ⚠️
(needs a real model — the harness refuses to judge from a fake), one week in
staging ❌.

**New prerequisite from Phase 4:** the drug-interaction refusal turned out to
sit inside the legacy branch, so the agentic engine bypassed it entirely. Fixed
— but **the other ten step-4/step-5 handlers have not been audited for the same
problem.** Deleting ~1,200 lines before that audit makes any equivalent gap
permanent and invisible. See C8.

### A3 — The drug-interaction refusal now fires without a database match
A behaviour change I made and you have not reviewed. `decisions-needed.md` §D5
has before/after examples. One-line revert if you disagree; I would push back,
because a false refusal costs a mildly unhelpful reply and a false *answer*
about a real interaction can hurt someone.

### A4 — Buy a drug-interaction dataset?
**My recommendation: not yet.** A half-covered table returns "no interaction
found", which reads as "no interaction exists" — a worse failure than today's
honest refusal, because the pipeline would be working correctly while producing
it. Full options table in `task-25-drug-interactions.md` §3.

### A5 — Build the per-user memory document? ✅ AGREED — and BUILT
And if yes, five sub-decisions (`per-user-memory.md` §8): how many past
documents to hold (rec: last 5 + trends), freshness tolerance (rec: 1 hour),
reuse `chat_personalization` consent (rec: yes), show the user what Davi
remembers (rec: yes, eventually), and whether 20% DAU / 6 turns per day is
realistic — every number scales linearly with that.

### A6 — Emergencies open no symptom episode ✅ FIXED
**Verified:** an emergency returns from the shared prologue before either engine
reaches the recording step, so the *most severe* red flags open no episode.
Pre-existing, on both engines. Arguably wrong — an emergency is exactly what is
worth remembering — but changing the emergency path is a safety decision, not a
refactor. Pinned by a test that fails loudly if the behaviour changes by
accident.

### A7 — `is_private=None` is treated as shareable
Consistent with the production listing filter, but the consequence is now
document *bytes* rather than a listing row. Worth a deliberate ruling.

### A8 — `job_runs` has no actor column ✅ FIXED (V21)
You learn a document was read, never by whom — the one field an access-control
audit exists for. `job_runs` is Flyway-owned, so this needs a migration adopted
into mhn-spring plus a coexistence re-check. Worth doing; not worth doing
quietly.

### A9 — Ratify the autonomous calls ✅ D1 ratified
`decisions-needed.md` D1–D10. The one worth your time is **D1**: `utcnow()` is
now strictly increasing. It fixed a real bug (compaction folding the wrong
turns, because 1,000 consecutive calls returned one distinct value and the
tiebreak was a random uuid4), but it changes every model's `created_at`. The
alternative — a sequence column — needs a Flyway migration coordinated with
mhn-spring.

---

## B. Blocked on credentials or infrastructure

None of these are decisions. Each is one command away once the thing exists.

| # | Blocked | Needs | Unblocks |
|---|---|---|---|
| B1 | Prompt-cache hit rate | An API key | The only honest number for A1. `scripts/cache_probe.py` is written and refuses to report a rate it did not measure |
| B2 | Provider bake-off (Task 13) | An API key and/or a self-hosted endpoint | Anthropic vs self-hosted — the harness and 27 cases are written |
| B3 | Real quality numbers | A real model | Task 12 gate condition 2 |
| B4 | Anthropic adapter live smoke test | An API key | Tested against a mocked SDK only |
| B5 | `pg` CI job + ordering job | CI actually running | **Neither has ever executed.** `_hybrid_rank` short-circuits off Postgres, so the whole hybrid retrieval path is untested |
| B6 | Document byte fetch (Task 17) | Spring exposing `GET /files/{type}/{id}/url` | Contract in `handover.md` |
| B7 | Vision (Task 18) | A vision model + `VISION_MODEL` | — |
| B8 | Voice (Task 19) | The ASR/TTS sidecar | A sidecar that reports no confidence pins every transcript at 0.0 — check before deploying |
| B9 | Drug interactions (Task 25) | A licensed dataset | See A4 |

---

## C. Findings that are real and not yet fixed

Ordered by when they bite.

### C1 — `forget_everything` covers 3 of 11 stores ✅ FIXED
**Verified** (`app/chat/profile.py:223,227,235`): it deletes `user_profiles`,
`user_memories` and `turn_feedback`. Episodes, insights, pedigree, sessions,
messages, summaries and receipts all survive a "forget me".

`conversation_sessions` cascades to messages **and** summaries
(`app/models/chat.py:76,89`), so **one additional `DELETE` takes coverage from
3/11 to 10/11.** This is the cheapest high-value fix on the list, and it must
land before any memory document exists — otherwise a rebuild reconstructs the
memory from sources the erasure never touched, and erasure becomes cache
invalidation.

### C2 — `list_sessions` scans the whole message table ✅ FIXED
**Verified** (`app/api/v1/chat.py:150-158`): a `GROUP BY session_id` over the
entire `conversation_messages` table with **no user predicate**, before
`LIMIT 50` can apply. It is a left outer join, so the filter cannot be pushed
into the aggregate. **Derived:** at ~100K users this times out on a UI endpoint.
Fix is a `LATERAL … ORDER BY created_at DESC LIMIT 1`. No migration.

### C3 — No retention on the transcript or the audit log ✅ FIXED
**Derived:** 9.94 TB/year at 10M users — 97.5% of Davi's per-user bytes — with
no cap, no retention and no delete path. `rag_turn_receipts` is 4.38B rows/year
and **no code path reads it**. A 180-day policy halves the pile.

### C4 — `nightly_sweep` runs in one transaction
**Verified** (`scripts/nightly_sweep.py::_main`): one session, one commit, a
serial `for user` loop. **Derived:** ~1.4 h at 1M users and ~14 h at 10M,
blocking autovacuum cluster-wide for the duration. Fix: keyset-paginate,
commit every 1,000 users. Trigger: sweep wall-clock > 30 min.

### C5 — Nine unbounded reads
**Reported by analysis, not individually verified by me:** nine `ORDER BY`
queries with no `LIMIT` that then take `.first()` in Python. `AsyncSession`
buffers, so every matching row crosses the wire to keep one. Cost scales with
**user tenure** — it worsens over exactly the period a user base grows. Sites
named in `per-user-memory.md` §7 rung 0.

### C6 — `clear_patient_context_memo` is barely called — STILL OPEN
> Previously listed as closed ("superseded by the document"). It is not: the
> memory document did not replace `_context_memo`, and the leak is unchanged.

**Verified:** `app/chat/context.py` defines it; `request_erasure` is now its
ONE production caller (added with the erasure fix), and the rest are in
`tests/test_turn_efficiency.py`. The comment at line 41 says it is "cleared
explicitly by `recompute_insights`' callers" — it is not.

`_context_memo` is a module-level dict keyed `(id(db), user_id)` that nothing
ever empties, so it grows one entry per session per user for the process
lifetime. **Not a cross-user PHI leak** — the key includes `user_id`, so a
recycled `id()` can only ever return that same user's entry — but it is an
unbounded leak and a stale-read hazard for that user.

### C7 — `analyze_image` holds a connection across S3 + a vision call
**Verified** (`app/chat/tools/executors.py:215,226` inside the savepoint at
`app/chat/tools/registry.py:84`): plausibly the longest single connection hold
in the system, and the one the new `ReleasingProvider` deliberately must **not**
release — committing there would destroy the tool's rollback isolation. Fixing
it properly means giving that executor its own short-lived session.

### C8 — Ten step-4/step-5 handlers unaudited
See A2. This is the Task 12 prerequisite.

### C9 — Audit rows are not durable
`_record` writes inside the caller's transaction, so a tool failure after a
successful fetch rolls the audit row back while **the read already happened**.
Fixing it properly means a separate short-lived session — the same shape as C7.

### C11 — Receipts are not PHI-free, and two docs say they are
**Verified.** `RagTurnReceipt.grounding` is a JSON column holding
`GroundingReport.to_dict()`, whose `violations[]` entries each carry
`"sentence"` — the offending **generated** sentence, verbatim
(`app/grounding/claims.py:168, 175, 181`). A grounding violation is exactly the
case where the model restated the reader's own numbers, so the stored text can
contain PHI.

Two places assert otherwise: `RagTurnReceipt`'s docstring ("Stores hashes,
never raw text") and CLAUDE.md's invariant, now annotated.

**Your call, because it changes what the audit trail holds.** Options: drop
`sentence` and keep `type`/`marker` (the audit signal survives, debugging gets
harder); hash it; or accept it and correct the two claims. I did not change
receipt contents unilaterally — that is an audit contract.

Retention limits the exposure either way: receipts are purged at 400 days.

### C10 — Two low-severity items, deliberately left
- `clinician_reviewers` carries both a unique and a plain index on `user_id`.
  Redundant, but consistent across Flyway and Alembic, so it is not drift and
  not worth a migration alone.
- The engine comment on changed facts states unconditionally that a replacement
  artifact goes to review. If the sensitive rule stops firing, the replacement
  is `active` and reaches the patient without review. Correct as designed;
  the comment overstates it. Left for a clinician to rule on.

---

## D. What is genuinely left

Nothing here is blocked and nothing here is a defect. This is the forward work,
in the order I would take it.

### D1 — The second cache breakpoint
**Ready, not wired.** The memory document's `prompt_block` is byte-stable —
`test_rendering_is_deterministic` and `test_an_unchanged_record_does_not_rewrite_the_block`
are what a breakpoint rests on, and both pass. Wiring it is a small change.

**Do not claim the saving — measure it.** `python -m scripts.cache_probe --model
claude-sonnet-5` against a real key. A breakpoint below the model's minimum
caches nothing **and returns no error**, which is exactly how the Haiku
constants were wrong for so long; the same failure mode is available here.

### D2 — Rebuild-on-write triggers
The nightly sweep rebuilds the document and `FRESHNESS = 1 hour` bounds the
staleness, so nothing is ever *wrong* — it is just late. A document uploaded at
14:02 should rebuild at 14:02 rather than waiting for the ceiling to expire.

The write points are already known: pedigree writes, profile updates, tracker
logs, and the mhn-ai processing callback. The work is calling `refresh` from
them without putting a rebuild on the request's critical path.

### D3 — The remaining reads, as tools
From [`whole-app-coverage.md`](./whole-app-coverage.md): **mood**,
**sleep/wearables**, **lifestyle aggregates**, and `report_parameter_value`.

These become **tools, not memory-document fields** — deliberately. They are
either fast-moving (wearables change hourly, and a document rebuilt hourly
would carry stale step counts as if they were current) or rarely asked for
(`report_parameter_value`), and the document is read on *every* turn. A field
costs tokens on all turns; a tool costs them only on the turns that need it.

### D4 — Two questions for the mhn-spring team
Neither blocks anything; both change what Davi is allowed to say.

1. **What does the medication `private` tickbox mean?** Davi currently treats
   it as "do not surface", which is the safe reading. If it means "hide from
   family" rather than "hide from the assistant", Davi is withholding the
   reader's own medication from the reader — including from the allergy check.
2. **Can the lifestyle rollups drift?** Davi reads the aggregates rather than
   recomputing from the logs, on the principle that two services must not show
   one phone two different numbers. That is only right if the rollups are
   authoritative and cannot lag their source rows.

---

## The order I would actually do this in

1. **A1 — pick the model.** Everything about caching is downstream of it.
2. **C1 — the one `DELETE`.** Cheapest high-value fix here, and a prerequisite
   for any memory work.
3. **`per-user-memory.md` rung 0** — the remaining commit/limit/memo items
   (C5, C6). ~60 lines, and the connection fix already landed.
4. **C2** at ~100K users. **C3 + C4** before 1M.
5. **A2's handler audit**, then Task 12 if staging is clean.
6. ~~**A5** — the memory document~~ **done.** Then **D1** (measure the cache
   breakpoint against a real key), **D2**, **D3**.

C7 and C9 are the same fix (a short-lived session for out-of-band writes) and
are worth doing together, whenever a connection-pressure symptom appears.


---

## What changed after your decisions

| Item | Outcome |
|---|---|
| A1 | **Sonnet 5.** Haiku 4.5 could not cache at all — at 10M users caching is worth ~$1.97M/month, so the cheaper token price was the more expensive choice. `model-cost.md` |
| A6 | Emergency events are recorded **before** the emergency path exits, at the triage floor's severity, on both engines. No retrieval, no topics — emergency handling does not continue through the normal assessment flow. |
| A8 | `job_runs.actor_user_id`, nullable (scheduled work has no actor; NULL means "the system"). No FK, so it outlives the account it attributes. |
| A9/D1 | Ratified, with the coverage you asked for: context assembly, compaction's `covers_through_message_id` under a same-tick burst, and thread safety. |
| C1 | Complete **deferred** erasure — all 11 tables, 30-day cancellable window, and the data stops being *used* immediately. |
| C2 | The aggregate is scoped to the caller before it groups. |
| C3 | Two windows: messages 180 days, receipts 400. Keep the evidence, drop the content. |

### A5 is built

The per-user memory document exists (`app/memory/document.py`,
`app/models/memory_document.py`), measured at 91 tokens against a 900 ceiling
that a test enforces. What `per-user-memory.md` staged as steps 1 and 2 is what
remains: history into `messages`, and the second cache breakpoint — now **D1**
above.

### One decision this work surfaced

`insight_review_audit` currently **survives** an erasure, on the reasoning that
it records which clinician read whose data and exists to protect the subject.
That is defensible and it is also the subject's own data. If you would rather
it be erased with everything else, it is one line in
`app/chat/erasure.py::_ERASE_IN_ORDER`.

# Handover

> **Resuming after a compaction?** Read this file, then
> [`open-items.md`](./open-items.md) for what is outstanding and
> [`memory.md`](./memory.md) for the invariants. Do not resume from a summary —
> several defects in this project came from acting on a plausible-but-wrong
> assumption about the repo, and these documents exist to prevent exactly that.

**Branch:** `praveen-mhn`, **59 commits ahead of `main`**, tree clean.
**Verified:** 1,863 passed · clean under shuffled seeds · ruff clean ·
pyright 0 · `run_evals` **17/17 on both engines** · coverage ~91%.

---

## ⚠️ The one thing to do first

**Apply `db/flyway/V20__davi_chat_platform.sql` into mhn-spring.**

It is a single idempotent file containing every outstanding Davi schema change:
`user_profiles`, `turn_feedback`, `clinician_reviewers`,
`insight_review_audit`, `erasure_requests`, `user_memory_document`,
`job_runs.actor_user_id`, and the retention indexes.

It is V20 because **Davi's old V7–V10 collided with mhn-spring's chain** — those
numbers were already taken by `medical_history`,
`medical_history_date_order`, `period_pause_and_pregnancy` and `ai_name_check`.
None of Davi's post-V6 migrations had ever been adopted, so this was a
renumbering rather than an incident. Before adding another, check mhn-spring's
head; `tests/test_flyway_parity.py` now does this automatically when the
sibling checkout exists.

Nothing else on this branch can be validated end-to-end until that file is
applied.

---

## What is on this branch

### Safety fixes — these are the reason not to sit on it

| | |
|---|---|
| **mhn-spring's V18 made three answers wrong** | Davi matched metrics to reference parameters by unanchored substring. V18 populated that catalogue, so `ldl` matched *"HDL/LDL Ratio"* (an LDL of 190 graded **normal**), `hdl` matched *"CHOL/HDL ratio"* (every HDL routed to **urgent care**), `hemoglobin` matched *"Glycated Hemoglobin (HbA1c)"* (anaemia graded **high**, in %). Now exact-name matching, with unapproved rows excluded. |
| **A drug reply ignored the reader's allergies** | The drug handler returns before the `[P]` block is built, and is legacy-only — so a severely penicillin-allergic reader asking about amoxicillin got a clean monograph. `build_drug_reply` now takes the warning. |
| **The interaction refusal bypassed the agentic engine** | Moved into the shared prologue. |
| **A commit inside a savepoint** would have made ~9,500 synthetic chats permanent in the shared database. Guarded. |

### Everything else

- **Sonnet 5** chosen (Haiku 4.5 cannot cache our prefix at all). Costs in
  [`model-cost.md`](./model-cost.md).
- **Deferred, complete erasure** — all 11 tables, 30-day cancellable window,
  and the data stops being *used* immediately.
- **Retention** — messages 180 days, receipts 400. Keep the evidence, drop the
  content.
- **The connection is no longer held across the LLM call.**
- **Per-user memory is shared by both engines** — it used to be split, so the
  consent-gated profile was never read on the default engine.
- **The memory document (A5)** — measured at 91 tokens for a typical reader,
  ceiling 900, and it cuts memory assembly from 6 queries to 2.
- **Cycle data**, gated the way mhn-spring gated it (own-data-only, no fertile
  window unless enabled, no predictions).
- **Adherence asked of Spring**, never recomputed.
- **`existing_schema.sql` refreshed** from V1–V19, and the coexistence check
  that CLAUDE.md always described now actually exists.

---

## Where to pick up

Ordered. Each is independently useful.

| # | What | Why it waits for the PR |
|---|---|---|
| 1 | **Second cache breakpoint** on the memory block | The block is byte-stable and ready. Verify the saving against a real key rather than claiming it — `python -m scripts.cache_probe --model claude-sonnet-5`. |
| 2 | **Rebuild-on-write triggers** | The nightly sweep rebuilds and a 1-hour freshness ceiling covers the gap, but an upload at 14:02 should rebuild at 14:02. |
| 3 | **The remaining reads** — mood, sleep/wearables, lifestyle aggregates, `report_parameter_value` | Tools, not document fields: fast-moving or rarely needed. Plan in [`whole-app-coverage.md`](./whole-app-coverage.md). |
| 4 | **Task 12** — retire the regex chain | You are running staging. **Audit the other ten step-4/step-5 handlers first** — two engine-split bugs have already been found there. |

---

## Two habits this session earned

**Measure, do not assert.** The memory document silently omitted every
document — `_gather` passed table names where kind keys were wanted, a
`KeyError` swallowed by a fail-open. Every test passed. It surfaced only when
the builder was run against real data to measure the block.

**Fail-opens hide defects.** That is twice now. When adding one, ask what it
would conceal.

---

## Integration contracts still to be honoured

**Spring must expose** `GET /files/{resource_type}/{id}/url` returning
`{"url": "https://…"}`, honouring `Authorization: Bearer <MHN_SPRING_TOKEN>`
plus `X-User-Id`. See `docs/production_integration.md`.

**Adherence** is read from `GET /medicine/courses/{trackingId}/adherence` —
already implemented against Spring's semantics (30-day, inclusive, user
timezone, PRN excluded).

**The voice sidecar must expose** `POST /transcribe` returning
`{text, language, confidence}` and `POST /speak` returning `{audio}` (base64).
A sidecar that reports no confidence pins every transcript at 0.0, which routes
everything through the confirmation path — safe, but useless.

**Synthesis is implemented but not wired** — `ChatResponse` has no audio field.

## Before touching the voice endpoint

Its first version returned the low-confidence confirmation with
`risk_level=NONE`, so a spoken "I can't breathe" at poor ASR confidence got a
chatty clarification instead of an emergency directive. ASR confidence
collapses on exactly the speech that signals an emergency — breathless,
panicked, pained — so the gate fired hardest on the people it most needed to
protect.

The floor now runs on the transcript unconditionally, and a red flag escalates
*before* the confirmation is appended. **Do not reorder that.**

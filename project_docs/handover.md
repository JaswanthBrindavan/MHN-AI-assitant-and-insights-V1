# Handover

> **Resuming after a compaction?** Read this file, then
> [`open-items.md`](./open-items.md) for what is outstanding and
> [`memory.md`](./memory.md) for the invariants. Do not resume from a summary —
> several defects in this project came from acting on a plausible-but-wrong
> assumption about the repo, and these documents exist to prevent exactly that.

**Branch:** `praveen-mhn`. **Verified:** 3,020 passed · 7 pg-deselected ·
clean under shuffled seeds · ruff clean · pyright 0 · `run_evals` **17/17 on
both engines**.

---

# ⇢ START HERE — do these before anything else

`project_docs/audit.md` (2026-09-01) replaced `drawbacks.md` and is now the
standing risk register: 80 verified findings across safety, security, data
integrity, reliability and product gaps. Its §"Recommended order of work" is
the plan. The short version, in order:

| # | Do this | Why first |
|---|---|---|
| **0** | **Run the nightly sweep once, by hand.** Actions → *nightly sweep* → Run workflow (needs repo secrets `DAVI_BASE_URL`, `DAVI_SERVICE_TOKEN`). | `SELECT … FROM job_runs WHERE name='nightly_sweep'` returns **0 rows**. It has never run. So **no erasure request has ever completed**, retention has never run, and `user_memory_document` is empty. Watch it: the first run clears the entire accumulated backlog in one batch against a database two other services share. |
| **1** | **Triage recall for plain-language emergencies** — audit **C1**, **C2**. | The only class where one ordinary sentence produces a clinically wrong answer with nothing behind it. *"one side of my face has gone droopy and i cant lift my arm"* measures **none** today. Note C1's headline in the audit was overstated and is corrected in place — read the correction box before acting. Needs clinician sign-off on new phrases; the symptom-combination tier does not, and generalises where a phrase list cannot. |
| **2** | **Make the output guards do what the docs claim** — **C4** (a four-site reorder), **C3**, **C7** (`GROUNDING_MODE` defaults to `log`). | Cheapest safety-per-line on the list. Turns three ornamental checks into real ones. |
| **3** | **The one-line correctness batch** — **D1**, **D2**, **S10**, **S3**, **R1**, **R6**. | Each under ten lines, each closes a defect that fails *silently*. One review instead of six. **R1**: the production LLM call has no timeout — 600 s × 3 attempts. **D1**: one failing account jams the whole erasure queue. **D2**: the sweep marks itself `succeeded` before the destructive work runs. |
| **4** | **`S1` — `APP_ENV` defaults to `"dev"`** so the refuse-to-start guard is inert, and `APP_ENV` appears in no deploy descriptor. | `AUTH_ENABLED=true` is the real control and is documented, so this is a disarmed backstop rather than a live hole — but it is the backstop for header impersonation on a service sharing the production database. |
| **5** | **Observability, before any structural work** — **R7** (no logging config at all: every INFO is discarded), **R8** (nothing scrapes `/metrics`), **R3**, **R9**. | Everything below this line is work whose success you currently cannot observe. |

**Two decisions waiting on the owner, not on an agent:**

- **Hinglish is answered in English.** `detect_language` is script-range based,
  so romanized Hindi never engages the pivot (audit **M6**). The fix needs a
  small set of high-frequency romanized function words as a *router* to decide
  whether to ask the sidecar — and `app/i18n/language.py` deliberately
  documents "NO word lists". That tension is a design call, not a bug fix.
- **`"crushing chest pain"` alone measures HIGH, not EMERGENCY**, despite
  being listed in `EMERGENCY_PHRASES`. Only the ACS co-occurrence rule reaches
  EMERGENCY. It may be deliberate. It has been flagged three times and left
  alone each time.

**Read the audit's own correction box first.** Two of its findings were
re-measured by hand before publishing and *both moved* — one was overstated,
one understated. A 0% refutation rate in the automated verification pass means
individual severities are a starting point, not a verdict. Re-measure before
acting.

---

## The schema is applied

Adopted into mhn-spring as **`V21__davi_chat_platform.sql`** — byte-identical
to what this branch produced. One idempotent file containing every outstanding
Davi schema change:
`user_profiles`, `turn_feedback`, `clinician_reviewers`,
`insight_review_audit`, `erasure_requests`, `user_memory_document`,
`job_runs.actor_user_id`, and the retention indexes.

**It has been renumbered twice, both times because of a collision.** Davi's
original V7–V10 were already taken by `medical_history`,
`medical_history_date_order`, `period_pause_and_pregnancy` and `ai_name_check`.
It was then staged as V20 — and mhn-spring added its own
`V20__staff_sessions.sql`, which `tests/test_flyway_parity.py` caught a day
after that guard was written.

**Flyway version numbers are a shared namespace this repo cannot see.** Before
adding another, check mhn-spring's head. The guard does it automatically when
the sibling checkout is present.

`db/` is now **gitignored** — mhn-spring owns these files. What remains here is
a staging copy; the parity guard stops it drifting from the models, and both it
and the coexistence check skip cleanly when the directory is absent rather than
failing a fresh clone. Regenerate the schema dump with
`python -m scripts.build_existing_schema`.

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

## Where to pick up (the older backlog)

Superseded in priority by START HERE above — these are the pre-audit items and
remain valid, just lower.

| # | What | Notes |
|---|---|---|
| 1 | **Second cache breakpoint** on the memory block | Byte-stable and ready. Verify against a real key rather than claiming it — `python -m scripts.cache_probe --model claude-sonnet-5`. |
| 2 | **Rebuild-on-write triggers** | The sweep rebuilds and a 1-hour freshness ceiling covers the gap — but the sweep has never run, so today nothing rebuilds at all. |
| 3 | **The remaining reads** — mood, sleep/wearables, lifestyle aggregates, `report_parameter_value` | Plan in [`whole-app-coverage.md`](./whole-app-coverage.md). See also audit **M5**: the reader's OWN diagnoses, surgeries and non-medication allergies are never read, while their family history is. |
| 4 | **Task 12** — retire the regex chain | **Audit the other step-4/step-5 handlers first.** Three bypasses of this class have now been found, the most recent by using the deployed app: a colloquial drug name ("my bp tablet") skipped the interaction refusal entirely. |

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

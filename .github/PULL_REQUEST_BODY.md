# Davi: safety fixes, mhn-spring V19 integration, and the per-user memory document

**The schema is already live.** Adopted into mhn-spring as
`V21__davi_chat_platform.sql`, byte-identical to what this branch produced,
merged to their `main`, and its tables are present in the staging database.
Nothing here could be validated end-to-end without it — that was the one
blocking step, and it is done.

---

## ⚠️ Three safety fixes, and one of them is live today

### 1. mhn-spring's V18 made three patient-facing answers wrong

`app/health/reference.py` matched a metric to a reference parameter by
**unanchored substring**, from a query with no `ORDER BY`. That was harmless
while `traditional_health_parameters` was empty — Davi fell back to its own
correct constants. **V18 is the first migration in the chain that populates
that table**, with 192 parameters whose names contain each other. And the
backend band beats Davi's constants unconditionally.

Reproduced against V18's real rows, in V18's own insert order:

| Patient says | Matched | Result |
|---|---|---|
| "my LDL is 190" | `HDL/LDL Ratio` (range 0.4–999, **draft**) | graded **normal** — statin-territory LDL, actively reassured |
| "my HDL is 45" | `CHOL/HDL ratio` (max 8.4) | graded **danger** → "seek medical advice promptly" |
| "my hemoglobin is 8" | `Glycated Hemoglobin (HbA1c)` (4–5.7 %) | anaemia graded **high**, wrong unit, direction inverted |

All three silent: a confident, well-formed, wrong answer. **Nobody had to do
anything — Flyway applies V18 and the answers change.**

Fixed with exact-name matching, deterministic ordering, and unapproved
(`status='draft'`) rows excluded. No match now falls back to Davi's own
constants, which is the safe direction.

### 2. A drug reply ignored the reader's own medication allergies

The drug handler returns from the orchestrator **before** the `[P]` block is
built, and sits **after** the engine branch — so it is legacy-only, and legacy
is the default. `build_drug_reply(drug)` took no user, so allergies were not
merely unread there: they were unreachable.

> A reader with a **severe penicillin allergy with anaphylaxis** asks *"side
> effects of amoxicillin"* — a penicillin-class drug — and gets a clean
> monograph. On `agentic` the same question includes the allergy.

Fixed with one parameter — now `build_drug_reply(drug, substitutes,
allergy_warning=...)` after the `medicine_master` merge below. The warning goes
first. `medical_condition` is now mapped, honouring `private`.

### 3. A commit inside a savepoint would have written ~9,500 synthetic chats

Four scripts wrap `handle_chat` in `begin_nested` + `rollback` so synthetic
traffic leaves no trace. The connection-release work would have released their
savepoint — making that traffic **permanent in the shared production
database** and then raising `ResourceClosedError`. Caught in review, guarded,
and covered by tests that fail if the guard is removed.

---

## Flyway: Davi's V7–V10 collided with mhn-spring's chain

Those numbers were already taken by `medical_history`,
`medical_history_date_order`, `period_pause_and_pregnancy` and
`ai_name_check`. Four migrations that could never have applied.

None had been adopted, so this was a renumbering rather than an incident.
Everything became one idempotent file, now **adopted into mhn-spring as
`V21__davi_chat_platform.sql`**.

`tests/test_flyway_parity.py` now checks Davi's numbers against mhn-spring's
actual migration directory when the sibling checkout is present — and **it
earned its place within a day**: the file was staged here as V20, mhn-spring
added its own `V20__staff_sessions.sql`, and the guard caught the second
collision before it shipped.

`db/` is gitignored on this branch. mhn-spring owns these files now; what
remains here is a staging copy, and the parity guard is what stops it drifting
from the models. Both it and the coexistence check skip cleanly when the
directory is absent rather than failing a fresh clone.

---

## Merged `main` — the `medicine_master` drug path (#21)

#21 rerouted the drug path from `drug_reference` to the Flyway-owned
`medicine_master` (V19) — the move `spring-integration-v19.md` recommended. It
lands on top of this branch's drug-path safety work; both survive. Two conflict
resolutions worth a reviewer's eye:

**Their step-5a interaction block was dropped, deliberately.** This branch moved
that handler into the shared prologue precisely because sitting at 5a made it
legacy-only — the defect in safety fix #3's neighbourhood. Re-adding it would
restore it. Their provenance change is carried into the prologue copy instead.
`interaction_never_guesses` passing on the **agentic** engine is what proves
nothing was lost. Their version also gates the refusal on `matched_any` where
the prologue version does not — that is open item **A3**, still yours to review.

**The agentic engine's `lookup_medicine` tool auto-merged broken** — no
conflict, because that file only exists on this branch, so #21 never saw it.
`drug.uses` no longer exists (pyright caught it), and `side_effects` became
`", "`-joined TEXT, so the old list slice silently truncated a **word**:
`"nausea, vomiting"[:5] == "nause"`, fed to the model as a fact. Valid Python.
Fixed and covered.

---

## Scaling and cost

**Sonnet 5** over Haiku 4.5. Haiku needs a 4,096-token cacheable prefix; ours
is ~2,541, so on Haiku prompt caching does nothing **and returns no error**. At
10M users caching is worth ~$1.97M/month — the cheaper token price was the more
expensive choice. Full model in `project_docs/model-cost.md` (~$0.47 per active
user per month, linear).

**A DB connection was held across every LLM call.** `chat.py` committed after
`handle_chat` returned, so an open transaction spanned the model round-trip.
Derived: ~167 concurrent connections at 1M users, ~1,667 at 10M, against a
default pool of 15 — on a database shared with mhn-spring and mhn-ai. Fixed
with a commit-placement change worth **120× the headroom**.

---

## Privacy and data lifecycle

- **Erasure is complete and deferred.** `forget_everything` reached 3 of 11
  tables; it now reaches all of them, on a 30-day cancellable window — and the
  assistant **stops using the data immediately**, which is what makes the
  deferral honest.
- **Retention**: messages 180 days, receipts 400. Receipts hash the message
  rather than storing it, so they are the audit trail without the PHI — keep
  the evidence, drop the content. Previously **nothing deleted either**
  (~9.94 TB/yr at 10M).
- **`job_runs.actor_user_id`** — you could learn a document was read, never by
  whom.
- **Cycle data** is in, gated the way mhn-spring gated it: own-data-only, no
  fertile window unless the reader enabled it, no predictions, and only
  pregnancy/breastfeeding travels in the prompt.

---

## The per-user memory document (A5)

One row per user, assembled on write, read with a single lookup. **Measured**:

| | |
|---|---|
| Prompt block | **91 tokens** (ceiling 900, enforced by test) |
| Memory assembly | **6 queries → 2** |
| SELECTs per turn | **26 → 22** |

Only the reader's **own** data — family permission is checked live, and a
document that absorbed a relative's result would survive the revocation that
should have removed it. Falling back to live assembly is always safe.

---

## Also fixed along the way

- Per-user memory was **split across the two engines** — the consent-gated
  profile was never read on the default engine, and no symptom episode was ever
  recorded.
- **Emergency events** are now recorded before the emergency path exits.
- **Coverage was under-counting async code project-wide** (SQLAlchemy runs
  awaited calls in greenlets) — real total was 91.4%, not 88.9%.
- **`list_sessions`** grouped the entire message table before the caller's
  `LIMIT 50` could apply.
- **The coexistence check** CLAUDE.md always described **did not exist**. It
  does now, and `db/existing_schema.sql` is refreshed from V1–V19 — it was the
  V1 baseline, which is *how* V18 went unnoticed.
- Prompt-cache minimums are **per model**; both our constants were wrong.

---

## Verification

```
1,863 passed (and under shuffled seeds)
ruff clean · pyright 0 errors
run_evals 17/17 on BOTH engines
coverage ~91%
```

Every safety fix is mutation-checked — reverting it fails its tests.

**Not verified here:** anything needing a live API key (cache hit rate,
provider bake-off, real quality numbers) or a PostgreSQL (`pg`-marked
migration and coexistence tests). The CI Postgres job is where those run.

---

## What comes next

Ordered in `project_docs/handover.md`. Briefly: the second cache breakpoint
(the memory block is byte-stable and ready — verify the saving against a real
key rather than claiming it), rebuild-on-write triggers, the remaining reads as
tools, then Task 12 — which needs the other ten step-4/step-5 handlers audited
first, since two engine-split bugs have already been found there.

**Open for the mhn-spring team:** what the medication `private` tickbox is
meant to mean, and whether the lifestyle rollups can drift.

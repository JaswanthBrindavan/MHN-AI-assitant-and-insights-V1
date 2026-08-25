# Phase 4 findings (Tasks 22–25)

Every finding, including the ones that turned out to be wrong. Ordered by what
would have mattered in production.

---

## Confirmed defects, fixed

### F4-1 — The agentic engine never reached the drug-interaction refusal
**Severity: HIGH.** `app/chat/orchestrator.py`

`_dispatch_agentic` returns at step **3.5**. The drug paths — including the
deterministic "can I take X with Y" refusal — lived at step **5**, inside the
legacy chain. Under `CHAT_ENGINE=agentic`, an interaction question therefore
went straight to the model, which answered it from its own weights, on the one
question class where the codebase explicitly forbids that.

The comment above the engine branch said *"Everything above — the triage floor,
the scope guard, the emergency directive and the canned conversational replies
— is SHARED"*. It was accurate and it was incomplete: it enumerated what was
shared without anyone checking what *should* be.

**Found by:** the two new safety evals, run under `CHAT_ENGINE=agentic`. The
legacy engine passed 17/17 while agentic passed 15/17.

**Fixed:** the refusal moved into the shared prologue as `_interaction_refusal`,
returning `ChatResult | None`. Both engines now pass 17/17.

**Consequence for Task 12:** this is the strongest evidence yet for the gate on
retiring the legacy chain. The other ten step-5 handlers have *not* been
audited for the same problem, and this one was invisible until a scenario was
written for it.

### F4-2 — The interaction refusal only fired on a database hit
**Severity: HIGH.** `app/chat/orchestrator.py`, `app/drugs/service.py`

The refusal required `drug_reference` to recognise at least one of the two
terms. If it recognised neither, the question fell through to the LLM.

The gate's stated purpose was sound — keeping "honey and lemon" on the ordinary
path — but it fails in the wrong direction. Names that miss a 250K-row Indian
brand dataset include foreign brands, generic names, supplements, and above all
**misspellings**, which are most likely exactly when the reader is least sure
what they are taking.

**Fixed:** fires on the phrasing; recognition is recorded in
`provenance["recognised"]` rather than gated on; `NON_DRUG_TERMS` grew everyday
foods to keep food pairings on the LLM path. Full reasoning and worked
examples: `project_docs/task-25-drug-interactions.md`.

### F4-3 — Per-turn directives would have written into the cached prefix
**Severity: MEDIUM (cost), and it would have been silent.** `app/chat/agent.py`

Two paths appended instructions with `system + directive`: the
tool-budget-exhausted `_FORCE_ANSWER` and the corrective `recover()` retry. Once
`system` became a split prompt, both would have appended to element 0 — the one
string that must stay byte-identical — turning every corrected or
budget-exhausted turn into a cache miss for everything after it.

**Found by:** the existing suite, as a `TypeError: can only concatenate list
(not "str") to list`. Worth noting *why* it was loud: the split is a `list`. A
design using a marker string inside one `str` would have failed **silently**,
as a permanent cache miss nobody would investigate.

**Fixed:** `append_directive()` writes to the volatile tail. Used by both paths.

### F4-4 — A new metric would have rendered nowhere
**Severity: MEDIUM.** `app/telemetry.py`

`telemetry._ALL` is a hand-maintained tuple. The feedback counter was first
declared in `app/api/v1/feedback.py`, incremented correctly, and appeared in no
`/metrics` output at all. A metrics page missing your metric looks exactly like
one that includes it.

**Fixed:** declared in `telemetry.py` and added to `_ALL`; a test asserts the
counter reaches the rendered output, not merely that `inc()` was called.

### F4-5 — The CI `ordering` job could never have run
**Severity: MEDIUM.** `pyproject.toml`, `.github/workflows/ci.yml`

The job added in Task 28 runs `pytest -p randomly --randomly-seed=1`.
`pytest-randomly` was never added to the dev extras, so the job would fail at
startup with `Error importing plugin "randomly"`. A CI job that has never
executed is not a gate — it is a green checkmark for nothing.

**Fixed:** added to dev dependencies; suite verified clean under seeds 1, 2 and
12345.

### F4-6 — Nothing compared the Flyway files to the models
**Severity: MEDIUM.** `db/flyway/`

Per CLAUDE.md, production schema ships as Flyway (`V*__davi_*.sql`) while the
entire test suite runs against Alembic-built schema. The two were maintained by
hand, in parallel, with no check between them. A drifted column name would pass
every test in this repo and fail only in production, at runtime, as a missing
column.

**Fixed:** `tests/test_flyway_parity.py` compares column names and nullability
for every Davi-owned table, checks every `CREATE` is `IF NOT EXISTS`, and fails
if a new `V*__davi_*.sql` appears without coverage. V6 remains covered by the
coexistence test instead.

**Result of the first run:** V7 was already correct. No drift had occurred —
the guard is preventative.

---

## Design decisions worth stating

### D4-1 — `"suppressed"` had to become a LIVE status
`app/insights/engine.py`

`LIVE_STATUSES` is what the hash-supersede check compares against. Leaving
`"suppressed"` out of it would mean every nightly sweep created a fresh
`held_for_review` duplicate of an insight a clinician had already declined —
the same decision, demanded forever.

The counterpart matters as much: **changed facts still produce a new held
artifact.** A suppression must not become a permanent gag on a condition whose
evidence later changes.

Both are tested against the real engine (`tests/test_review_recompute.py`), not
by asserting the constant. Removing `"suppressed"` fails those behavioural
tests.

### D4-2 — A promoted quality case never carries the down-voted reply
`scripts/promote_feedback.py`

Writing the rejected reply into `scripted` would make the regression suite
protect the defect. The promoter writes the question and a crude `addresses`
seed; a human states what good looks like.

### D4-3 — The language directive belongs in the volatile half
`app/chat/orchestrator.py`

It varies with the reader's language. A per-reader prefix caches for nobody, so
putting it in the stable half would have cost a cache write per reader and
never yielded a read.

### D4-4 — Feedback and audit rows have no FK to what they describe
Both must **outlive** their subject. Clearing conversation history would
otherwise erase the evidence behind a regression test, and deleting a retracted
insight would erase the record of who read it — which is exactly the case where
that record matters most.

---

## Measured, and deliberately not claimed

### N4-1 — The cache hit rate is NOT measured
No API key in this environment. The plan's acceptance criterion explicitly
allows *"a written finding"* in place of a number, and that is what
`project_docs/task-23-caching.md` is.

What **is** measured: the prefix is ~2,541 estimated tokens (system ~850 + tool
schemas ~1,691). What is **not**: the exact count and the hit rate.

The important sub-finding: **the system rules alone are ~850 tokens, under the
1024 minimum.** Only the tool schemas in front of them carry the prefix over
the line. On Haiku (2048 minimum) the estimate is within the margin of error,
and `scripts/cache_probe.py` prints `WITHIN THE MARGIN OF ERROR` rather than
waving it through.

`tests/test_cache_probe.py` asserts the probe never prints a percentage it did
not observe — the same discipline the quality harness needed in Task 21.

### N4-2 — The expected cost saving is arithmetic, not a result
~85% off the prefix by the fifth turn, computed from published rates. Not a
measurement, and not quoted as one.

---

## Considered and rejected

- **A `--force-cache` flag or config toggle for the breakpoint.** Nothing would
  ever set it differently. The split is either correct or it is not.
- **Storing the down-voted reply text on the feedback row.** Tempting for
  debugging, but it duplicates `conversation_messages` and creates a second
  copy of PHI with its own retention question. The `message_id` is enough.
- **A cross-user maintainer view on `/feedback/review`.** Different
  authorization, different audit requirements. It belongs with the clinician
  queue, not bolted onto a per-user endpoint.
- **Building `drug_interactions` + an ingest script now.** The schema, the
  identifier scheme and the severity vocabulary all come from whichever dataset
  is licensed. Building for a hypothetical one is scaffolding that gets thrown
  away.
- **Adding an `active` check to `authorize_user`.** Reviewer standing is a
  different concept from object ownership; conflating them would have made
  every ordinary endpoint consult the reviewer table.

---

## Verification performed

| Check | Result |
|---|---|
| Full suite | **1718 passed**, 4 deselected |
| Random order | clean under seeds 1, 2, 12345 |
| ruff | clean |
| pyright | 0 errors |
| Safety evals, legacy | **17/17** |
| Safety evals, agentic | **17/17** |
| Quality evals | legacy 90.9%, agentic 100% (tool choice unscored — fake provider) |

**Mutation checks run** (each mutation applied, suite run, mutation reverted):

| Mutation | Caught |
|---|---|
| Reviewer check dropped from the queue | yes |
| Revocation (`active`) ignored | yes |
| Held-only gate dropped from view | yes |
| Double-decision guard dropped | yes |
| Audit write on view removed | yes |
| Audit-trail scoping dropped | yes |
| `"suppressed"` dropped from `LIVE_STATUSES` | yes — **behaviourally**, via the real engine |
| Feedback counter unregistered from `_ALL` | yes |
| Feedback review-queue user filter dropped | yes |
| Feedback triage ownership check dropped | yes |
| Flyway column dropped / nullability flipped | yes |
| Interaction gate reverted to requiring a DB hit | yes — 6 of 12 |
| Shared interaction call removed | yes — 8 of 14 |

Reviewers remained **read-only** throughout, per the process fix recorded in
Phase 3.

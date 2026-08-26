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

---

# Phase 4 review round (read-only reviewer)

A read-only review agent went over all four Phase 4 commits. It found real
things, including one regression I had introduced hours earlier and one class
of bug that had been silently degrading the coverage gate for the whole
project. Everything below was **verified before being acted on**.

## Confirmed and fixed

### R-1 — "Can I take my medicine with food?" got a nonsense refusal
**Severity: HIGH — a regression I introduced in Task 25.**

Hardening the interaction gate to fire on phrasing meant one unrecognised term
was enough. `extract_interaction_query("Can I take my medicine with food?")`
returns `("medicine", "food")`; `"food"` is exempt, `"medicine"` was not — so
an extremely ordinary question produced *"Whether medicine and food can be
taken together depends on the doses, the timing…"*.

That is worse than the LLM answer it replaced. The asymmetry argument for
hardening the gate ("a false refusal is merely unhelpful") holds for real drug
names; it does not hold when **neither** term is a drug and the reply is
gibberish.

**Fixed:** generic nouns for "a medicine" (`medicine`, `medication`, `tablet`,
`pill`, `capsule`, `drug`, `syrup`, `supplement`, `vitamin`, `painkiller`,
`antibiotic`, and plurals) joined `NON_DRUG_TERMS`. The refusal now needs at
least one side to name a **specific** substance. *"Can I take paracetamol with
my medicine?"* still fires — correctly, since one side is specific.

### R-2 — A negative `limit` defeated every row cap
**Severity: MEDIUM.** Three new endpoints used `.limit(min(limit, N))`, which
clamps only the upper bound.

Verified directly: `LIMIT -1` on SQLite returns **every row** (measured: 5 of 5
with a limit of −1). So `GET /review/queue?limit=-1` returned the entire held
queue for every patient — bypassing the cap that exists to stop that endpoint
being a bulk-disclosure surface. On PostgreSQL the same request is an
unhandled 500.

**Fixed:** `Query(default=…, ge=1, le=…)` on all three.

### R-3 — `forget_everything` never deleted feedback
**Severity: MEDIUM.** `app/models/feedback.py` states the comment field *"is
erased by the same 'forget me' path as the profile."* It was not. Nothing in
the codebase deleted a `TurnFeedback` row.

A reader could down-vote with *"you were wrong about my HIV meds"*, call
forget-me, and have that text persist against their user id indefinitely —
while the file a reviewer would check to verify the behaviour asserted the
opposite.

**Fixed:** `forget_everything` now deletes feedback and reports the count.

### R-4 — `GET /review/audit` wrote no audit row
**Severity: MEDIUM.** The module docstring claimed every endpoint audits. The
one endpoint that can return the **cross-patient** trail — reviewer ids,
subject ids, content hashes, and every clinician's free-text note — recorded
nothing. A reviewer under investigation could read the investigation surface
invisibly.

**Fixed:** a cross-patient or unscoped read writes an `audit_read` row. A
patient reading their *own* trail does not, and the docstring now states that
exception instead of overclaiming.

### R-5 — The queue listing was audited to the reviewer, not the patient
**Severity: MEDIUM.** A listing carries `user_id` + `condition_code` + a title
like *"Family history of type 2 diabetes"* — a patient bound to a named
condition, which is a disclosure. The single audit row was filed under the
**reviewer's** id, so it never appeared in the patient's answer to "who looked
at my records?" — the question the subject index exists to answer.

**Fixed:** one row per **distinct patient** in the page. Bounded by page size.

### R-6 — Clinician decision notes were returned verbatim to patients
**Severity: MEDIUM.** `note` is 1,000 characters a clinician writes for other
clinicians. *"Patient is highly anxious, this will spiral them"* is a
defensible note and an indefensible thing to hand the patient unannounced.

**Fixed:** patients reading their own trail see **that** a decision was made
and when; the reasoning stays professional correspondence. Reviewers still see
it in full.

### R-7 — A reviewer could decide on their own held insight
**Severity: MEDIUM.** `_decide` never compared reviewer to subject.
Independent review is the entire premise of the roster, and the audit row would
have recorded reviewer and subject as the same person — a record of the control
not working.

**Fixed:** 403. Two tests whose setup made the reviewer own the artifact were
rewritten to use a separate clinician identity, which is the realistic
arrangement anyway.

### R-8 — The budget costed turns at full length; the renderer truncates to 400
**Severity: MEDIUM.** `format_recent_turns` renders `text[:400]`, but
`_fit_budget` charged the whole message. Six 4,000-character turns "cost"
~6,900 tokens against a 6,000 budget and render as ~690 — enough to evict
**every retrieved chunk** from a health question because the reader had earlier
pasted a long lab report.

**Fixed:** `TURN_RENDER_LIMIT = 400` is now shared by the renderer and the cost
function, with a test that fails if they diverge.

### R-9 — The fail-open in `_interaction_refusal` swallowed the refusal itself
**Severity: MEDIUM.** The `try` wrapped the receipt write and the `ChatResult`
construction, not just the parse and the lookup. A transient database error
inside `_write_receipt` therefore returned `None` — handing *"can I take
warfarin and aspirin together?"* to the LLM. That is precisely the outcome the
commit claimed to eliminate, reachable by a database hiccup.

**Fixed:** narrowed to two small blocks. The parse failing returns `None`
(nothing was asked); the recognition lookup failing costs only the statistic —
the refusal proceeds either way. `_write_receipt` is already fail-open
internally.

### R-10 — `append_directive` could write into the prefix
**Severity: LOW (latent).** A one-element sequence made `parts[-1]` the prefix,
which is exactly what the function exists to prevent; an empty one raised
`IndexError`.

**Fixed:** fewer than two elements degrades to a plain string — no breakpoint,
no silent corruption.

### R-11 — V8's partial index existed only in Flyway
**Severity: LOW.** Production would have an index no local or CI database ever
built, so the review-queue query was planned differently in test than in
production. **Fixed** in the Alembic revision with both dialect predicates.

### R-12 — Two comments described a different implementation
**Severity: LOW, but the caching one is a trap.** `anthropic.py` said the
breakpoint goes on the **LAST** system block; the code marks the **first**.
Both happen to cover the tools, so the code was right — but the next person to
"fix the code to match the comment" moves the breakpoint onto the volatile tail
and silently ends caching. The comment now says which block and why, and
explicitly warns against that change.

`models/rules.py` still documented the status column as
`active | superseded | held_for_review`, missing `suppressed`.

### R-13 — Two tests that could not fail, and one mechanism with no test
**Severity: MEDIUM — this is the one that matters most.**

- `test_the_budget_drops_chunks_before_conversation_turns` seeded **one** turn.
  The trim loop is guarded by `len(kept_turns) > 1`, so a single turn is
  structurally undroppable and the assertion held against any implementation.
  Rewritten with three turns.
- **Nothing tested that the orchestrator actually ships a split prompt** — the
  mechanism the entire caching feature rests on. `FakeProvider` calls
  `join_system()` before recording, so every spy in the suite sees a flat
  string. Verified the reviewer's claim by mutation: joining the prompt in the
  orchestrator killed caching outright and **passed the whole suite**. Two new
  end-to-end tests now fail on exactly that mutation.
- `provenance["recognised"]` was only ever asserted `False`, guaranteed by an
  empty `drug_reference`. A positive-direction test now seeds a drug.

### R-14 — Coverage had been under-counting async code project-wide
**Severity: MEDIUM, and pre-existing — not a Phase 4 defect.**

Chasing an implausible 54% on the new feedback endpoint (40 passing tests, and
a `raise` inserted into the "uncovered" region failed them immediately) led to
the cause: **SQLAlchemy async runs each awaited DB call inside a greenlet**, and
coverage does not follow a greenlet switch with the default thread tracer. Every
statement after the first `await db.execute(...)` in a request handler was
reported as unexecuted.

```
app/api/v1/feedback.py   54% -> 98%
app/api/v1/review.py     69% -> 98%
TOTAL                 88.87% -> 91.38%
```

This means the 80% gate has been measuring less than it appeared to since it
was introduced, and any genuine gap in async DB code was indistinguishable from
the artefact. **Fixed:** `concurrency = ["thread", "greenlet"]`.

## Accepted as documentation, not code

### R-15 — The ~2,541-token prefix does not apply to no-tools calls
Sharp catch. Anthropic caches `tools → system → messages`, so a call with
`tools=()` has a prefix of the system rules **alone — ~850 tokens, under the
minimum**. Three paths do that: raised-risk turns, the forced answer after the
tool budget, and the corrective retry. So `append_directive()` cannot preserve
a cache hit on either path it was written for.

It is still correct and still worth having — its job is to stop a directive
being written into the prefix, which would poison caching for every subsequent
tools-bearing turn. But §2 of the Task 23 doc implied more than that.

**Recorded as a correction in `task-23-caching.md` §5** rather than quietly
edited, along with the option not taken (`tool_choice: none`, rejected because
it means provider-specific branching in the provider-neutral module).

### R-16 — The promoter writes patient free text into a git-tracked file
Real, and "forget me" cannot reach git history. A warning now heads the module,
pointing at `--dry-run` and asking the operator to generalise the wording before
committing.

### R-17 — A service token can present as a reviewer
Pre-existing auth design, not introduced here: with `SERVICE_TOKEN` set,
`auth.py` accepts the token plus `X-User-Id` as identity. The V9 comment said
the roster is *"the ONLY thing"* standing between a user id and cross-user
access. Now recorded in `models/review.py`: the roster protects against an
ordinary user, not against a leaked service token.

## Reported and not acted on

- **Step 3.4 ordering** — the refusal now runs before the deterministic data
  abilities. The reviewer could not construct a colliding input from the four
  interaction patterns and flagged it as unproven; I could not either. Noted,
  not changed.
- **`clinician_reviewers` has both a unique and a plain index on `user_id`.**
  Redundant but consistent across Flyway and Alembic, so it is not drift. Not
  worth a migration on its own.
- **`test_suppressed_is_a_live_status` asserts a constant.** True. It sits
  beside `test_review_recompute.py`, which proves the same property against the
  real engine, so it is a cheap signpost rather than the evidence. Left as is.
- **Engine comment on changed facts** — if the facts change such that the
  sensitive rule stops firing, the replacement artifact is `active` and reaches
  the patient without review. Correct as designed (a non-sensitive insight was
  never review material), but the comment states it unconditionally. Left for
  a clinician to rule on rather than changed unilaterally.

## Process note

The reviewer was **read-only**, per the rule adopted in Phase 3 after review
agents with write access left scratch files that produced false failures in
unrelated safety tests. It found more, and cost less, than the write-enabled
rounds did.

Two of its findings were **wrong to act on as stated** and were checked before
being believed — the "shadowing bug" was already fixed, and the `_fit_budget`
infinite-loop and negative-budget concerns were disproved by reading the loop
guards. Both are recorded above as reported-and-refuted rather than silently
dropped.

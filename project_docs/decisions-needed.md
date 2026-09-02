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

---

# Session of 2026-09-01/02 — wearables, health summary, correlations

Everything below was decided autonomously overnight, as instructed. Each took
the recommended option and kept going. Nothing here is blocking; if you
disagree with any of it, say so and it will be changed.

---

## D11 — the reference-range `danger` tier was dropped, not re-wired

**What changed.** mhn-spring's V28 dropped `thp_age_range.low_danger` and
`high_danger`. Davi still mapped both `NOT NULL` and branched on them, so
**every** backend range lookup raised `UndefinedColumn`, was swallowed by a
bare `except`, and silently fell back to the DRAFT constants. Grading is now
two-zone on `low_warn`/`high_warn`, per your instruction.

**Why the tier was not re-pointed at the surviving out-of-range zone.** It read
the two dropped columns, and V18 pinned `low_danger == min` and
`high_danger == max` on every row, so on the high side it only ever fired past
the end of the chart. Re-pointing `seek_care_promptly` at the surviving zone
would have sent an LDL of 105 to urgent care. Over-escalation is its own harm,
and it would have put this path out of step with every other one — an abnormal
report flag already routes to `discuss_with_clinician`.

**What it costs you.** No number on the value-check path can now produce
anything above "consult your doctor". A reviewer argued the low side of the old
tier was reachable (haemoglobin 4.4, glucose 40) and that triage does not catch
those phrasings. **That is a real gap and it is still open** — see D12.

**Verified against the live database**, not just the schema file: 277 rows, all
five surviving bounds `NOT NULL`, `low_warn <= high_warn` on every row.

---

## D12 — HDL graded backwards and the sex band was arbitrary — NOW FIXED (800f9cd)

**This is the one to read first.** The V28 fix switched the backend range path
ON for the first time. That makes two latent bugs live.

**HDL.** Davi's DRAFT constant is `RangeSpec("mg/dL", 40, None)` — a floor and
**no ceiling**, with the note "Higher HDL is generally better". Production's
catalogue gives HDL a `high_warn`: male 40–60, female 50–70. So a male HDL of
65 — a good result — is now told it is *above the usual range, consult your
doctor*. Confirmed against the live database.

**`ThpAgeRange.sex` is unmapped.** 78 rows across 28 parameters are
sex-specific, and the band is selected by age only, ordered by `age_min` with
no tiebreak — so the choice between a male and female band is arbitrary. HDL is
one of those parameters, so the two bugs compound: male 40–60 vs female 50–70.

**Why it shipped in PR #46 anyway.** You said V28 was fine because staging was
working. Staging was running the *old* code, where the lookup always threw and
fell back to DRAFT — so it could not have shown you this. I flagged both in the
PR body under "review these before merging" rather than blocking the merge you
asked for.

**Also found, and not ours to fix:** `HDL/LDL Ratio` in your reference
catalogue has `ideal = 499.7, high_warn = 999`. That is not a plausible ratio.
Someone should check the V18 import.

### Fixed in 800f9cd

**The direction** comes from Davi's own DRAFT spec, which already encodes it —
`hdl` is `RangeSpec("mg/dL", 40, None)`, "no upper bound is flagged". The
curated spec now decides which SIDES may warn; the backend still decides where
the line sits. This also corrected SpO2 (100% no longer warns) and the bottom
end of LDL and total cholesterol. A metric with no DRAFT spec keeps both sides.

**The band** is now picked by the reader's own sex, then the unisex band, and
never the other sex's. `reader_bands()` fetches age and sex in one query
because this path has no budget headroom. `gender = other` is treated as
unknown, since the catalogue seeds `any`/`female`/`male` only. Where a
parameter has sex-specific bands and the reader's sex is unknown, the lookup
returns None and the caller falls back to the DRAFT constants — grading
someone against the other sex's range is worse than a general one.

Nine tests in `tests/test_reference_sex_and_direction.py`, including the one
that matters: an HDL of 45 warns for a woman and is normal for a man.

---

## D13 — the chat `visual` payload names the metric, never the screen

**What changed.** `visual` now carries `source`, `metric`, `grain` and
`window_days`. A client maps `sleep_duration` to its own sleep-trend screen.
Davi will never send a route, screen name or deep link.

**Why, when the obvious design is to send the destination.** Three clients,
three navigation models: Android NavGraph routes, an iOS NavigationStack that
**does not exist yet** (there is no chat screen in mhn-ios at all), and web
URLs. Davi would own three route tables and break silently the first time any
of them renamed a screen. And phones cannot be updated in lockstep with the
server: a route the server knows and an older build does not is a dead tap,
whereas an unrecognised *metric* degrades to "renders, but not tappable".

`metric` is also already shared vocabulary — it is Sahha's own key, stored
verbatim in `sahha_biomarker.type`, deliberately `varchar` and not an enum
because Sahha keeps adding metrics, and it is what mhn-spring's
`SahhaMetricCatalog` is keyed on.

**What it costs you.** Each client writes a ~10-line metric-to-route map. The
tap-through behaviour you asked for is unchanged.

**Contract:** `project_docs/chat-visual-payload-contract.md`.

---

## D14 — every period gets a slot; `null` means absent and `0` means measured

**What changed.** `chart_payload` values are `list[float | None]`. A seven-day
window sends seven entries. An absent reading is `null`; a genuine zero is `0`.
The SVG skips a null bar rather than drawing it at zero.

**Why this overrode the spec.** The Sahha spec said to *omit* absent buckets.
Both mobile clients independently say the opposite, in their own words:
Android's `BarDatum.value: Double?` — *"Null for a period with no reading —
drawn as a stub, never as a zero"* — and iOS `TrendChart.swift` Rule 2 — *"the
run of slots is the axis, so every slot must be present"*. Omitting silently
shortens the week. Two clients written separately outvote the spec.

**One thing the iOS team needs to know.** Their `TrendPoint.value` is a
non-optional `Double` with `isEmpty = value <= 0`, justified as *"A total of
zero and a day that never synced are the same thing here."* That is true for
manual trackers and **false for wearables** — 0 steps on a synced day is a real
reading. Sending `null` is strictly more information; iOS can map it to a stub
today and refine later with no server change.

---

## D15 — correlations ship as a co-occurrence readout, and the contract was amended to allow it

**What changed.** You asked for correlations twice. I argued against them twice.
Built as a deterministic co-occurrence readout over a 28-day window — no model
call, no coefficient, no p-value — with a minimum-sample gate that refuses
honestly below threshold, wording that is explicitly non-causal, and a hard ban
on involving any medication.

**Why the binding contract had to change.** My own
`chat-visual-payload-contract.md` said Davi would never send "a causal or
correlational claim", and a reviewer correctly called the feature forbidden by
the document the task declared binding. I amended that clause rather than drop
the feature: the harm is in the **inference**, not in showing someone two of
their own records side by side. The amended section states the carve-out and
its conditions explicitly.

**Reverse it by** deleting the carve-out paragraph and the correlation handler
together — they were written to stand or fall as one.

---

## D16 — `lifestyle_daily_total.log_type` was renamed by mhn-spring and Davi never noticed

**What changed.** mhn-spring's V35 ran
`ALTER TABLE ... RENAME COLUMN log_type TO metric` on **four** rollup tables
and retyped the column. Davi still mapped `log_type`, so every read raised
`UndefinedColumn` in production. Fixed, and verified against the live database.

**The part worth your attention.** `tests/test_schema_parity.py` — the guard I
added earlier this same session, specifically to catch the other team moving a
column under us — replayed `ADD COLUMN` and `DROP COLUMN` and **not**
`RENAME COLUMN`. So the guard reported parity while production was broken. A
rename is the quietest way a column can move: nothing is added and nothing is
removed on net. The guard now replays renames too.

**This is the third instance of the same class** (V18 reference data, V28
dropped columns, V35 rename). The lesson is not "add another check" — it is
that `db/existing_schema.sql` must be regenerated whenever mhn-spring ships a
migration, because every check in this repository is downstream of that file.

**Also discovered:** the new `lifestyle_metric_enum` carries three metrics the
old one did not — `caffeine_mg`, `ethanol_g`, `drink_volume_ml`. mhn-spring
already pre-aggregates unit-safe totals as their own rollup rows, which is
almost certainly what Davi should read instead of re-summing `lifestyle_log`.

---

## D17 — the water-intake bug was a comma, and the guard was right to fire

**What you reported.** *"How's my water intake?"* answered with the careful
non-answer, while *"How my hydration this week?"* returned real data.

**What it actually was.** Not a parsing miss. The numeric-fidelity guard could
not read a thousands separator: `_UNIT_VALUE_RE` had no comma branch, so
`"14,000 ml"` tokenised to the fragment `"000 ml"`, which cannot be traced back
to a source holding `14000ml`. The guard correctly refused to pass a figure it
could not verify, the reply was replaced, **and the one corrective retry was
handed the nonsense fragment `"000 ml"` as the detail**, so it could not fix it
either.

**Why only water and alcohol.** They are the only metrics stored in `ml`, `ml`
is the only tracker unit the guard's vocabulary knows, and only their weekly
totals exceed 999. Coffee, tea, smoking, steps, sleep and HRV could never trip
it — which is also a quiet hole: a drifted cup count is invisible to the guard.

**Fixed at the tokeniser, not the validator.** Loosening the guard would have
been the wrong fix: it is the reason this class of bug is visible at all.

---

## D18 — the unit premise was inverted; the bug was in what Davi WRITES

**What we thought.** `SUM(quantity) GROUP BY log_type` was summing mixed units,
so a reader logging 2 glasses and 500 ml of water got 502.

**What is actually true**, settled from mhn-spring source rather than inferred:
`LifestyleMetric.java` defines one unit per metric; `resolveUnit` **rejects any
non-canonical unit with HTTP 400** — *"Totals are plain sums, so accepting a
second unit would silently add glasses to millilitres"*; and the reconciler
sums `l.quantity`. So `quantity` **is** the canonical measure and the read was
never wrong.

**The real bug was Davi's own writes**: `add_lifestyle_log` was putting
`quantity=2, unit='glass'` into a column mhn-spring reads as millilitres.
Fixed at the write with the sanctioned sizes from V35's `drink_serving_size`
seed, and Davi now refuses politely when a vessel has no sanctioned size rather
than guessing — the same thing mhn-spring's 400 does.

**A ~90-line read-side rewrite was deleted** once the premise was corrected.
The read collapsed back to what it always was.

**⚠️ Rows Davi already wrote before this fix** carry `unit='glass'` with
`quantity` in glasses. Nothing here can retroactively repair them. Davi now
reports the same number the app's own chart does, which is the best available,
but those rows are wrong in the shared table and you may want them cleaned up.

---

## D19 — derived figures are allowed through the fidelity guard, narrowly

**What changed.** "You logged 14,000 ml over the week — roughly 2,000 ml per
day" had the whole reply replaced, because the per-day figure appears in no
source. A value now traces if it equals a traced value divided by a *calendar*
divisor within 1%.

**Why not `range(2, 32)`.** Thirty divisors plus a tolerance is how a
hallucinated number lands on `total/n` by accident.

**A hole this opened, and how it was closed.** Divisors 2, 3 and 4 are also
dose-splitting arithmetic. With derivation allowed on clinical units, a source
holding "Metformin 500 mg" made **"Take 250 mg twice a day"** traceable, and a
blood sugar of 240 made **"your blood sugar was 120 mg/dL"** traceable — an
invented dose and an invented lab value, both passing the guard that exists to
catch exactly those. Derivation is now refused for `mg`, `mg/dL`, `iu`, `%` and
every other clinical unit: a per-day average is only ever asked of a period
total, never of a dose.

---

## D20 — a month or year ask says it only has a week

**What changed.** `yesterday`, `today`, `this week` and `last week` are real
calendar windows now. `month` and `year` are accepted and answered **with the
week, saying so**, rather than silently returning a week labelled "month".

**Why not implement them.** The wearable rollups have monthly buckets, the
lifestyle side does not, and the two would have disagreed about what a month
is. Less feature, no lie. Say the word and month/year become real.

**Not reconciled, deliberately.** "how much water" (rolling, reads the log) and
"how much water this week" (calendar, reads the rollup) can disagree for
today's entries, because Spring's reconciler rewrites a trailing window and
adding Davi's own same-day rows would **double count**. The reply discloses it
("today's logs are added when the daily totals compile overnight"), and the new
`today` window answers from the log directly.

---

## D21 — citations now say what the answer used, not what retrieval returned

**What changed.** `chunks` was the retrieval result sitting in scope at every
`return`, so a deterministic answer inherited whatever the retriever happened
to fetch — which is why your water answer cited MC369, MC131, MC044 and MC568
having consulted none of them. Replaced by `ChatResult.used`, threaded out of
each path *with* the answer. Default is "nothing from the corpus".

**Why structural rather than passing `None` at each site.** A rule enforced at
five call sites gets broken at the sixth — and the review found the sixth
before it shipped: the suggestions handler built its citation list from the DB
rows rather than from what the renderer emitted, and a test **pinned that wrong
behaviour**. Both fixed.

---

## D22 — three safety rules are enforced in the validator, with stated residual gaps

**What changed.** `wearable-grading`, `absence-as-finding` and
`personal-clearance` are rules in `find_banned`, which both engines pass
through — not instructions in a prompt the model may ignore.

**Why it needed two passes.** The first version blocked four of five ordinary
descriptive sentences while letting nine of the phrasings it targets through —
a guard that blocks safe prose and admits the unsafe sentence is worse than
none, because it trains everyone to route around it. Rewritten as four
conjuncts (the reader's own reading + a wearable metric + a figure + an
evaluative predicate). Now **9 of 9 unsafe phrasings blocked, 0 false positives
on 8 descriptive sentences**, including traffic lights and scores, which the
contract bans by name.

**Residual, and chosen:** a verdict with no figure at all ("that's a solid week
of sleep for you") still passes. Closing it means dropping the figure conjunct,
which blocks ordinary corpus prose like "sleep of 7–9 hours is healthy" — and
that is a feature.

---

## D23 — the per-turn query budget was raised for summary turns only

`MAX_QUERIES_PER_TURN` stays **28**; an ordinary turn still measures 28. A
health-summary turn measures **32**, so `MAX_QUERIES_PER_SUMMARY_TURN = 34` was
added with the measurement and the reasoning beside it.

Batched before raising: the per-metric wearable lookups became one
`wearable_latest()`, and the chart shares the wearable section's savepoint —
36 down to 32. Over half the remaining cost is savepoints, which are what buy
per-section fail-open, so a summary degrades one section rather than the reply.

---

## D24 — `heart_rate` was removed from the value-check tool's enum

The executor calls the handler with an **empty message**, so the wearable
refusal — which reads the reader's own words — cannot fire on the tool path.
While `heart_rate` was a legal value, *"my watch says my resting heart rate is
48"* refused on legacy and came back with a band on agentic: one deterministic
guard, reachable on one engine only. A clinic pulse typed by the reader still
reaches the handler through the legacy path, which can see "my watch".

---

## D25 — what was still open, and what happened to it

All four are now closed or handed over. Recorded because three of them turned
up something that was not in the original list.

**1. HDL and `ThpAgeRange.sex`** — fixed in 800f9cd. See D12.

**2. "does my metformin affect my sleep"** — fixed in fdd4647. The catalogue
check runs in the handler (the parser is pure and cannot query), gated behind
`medication_candidates()` so an ordinary turn spends no query, and it checks
the reader's own medication list before `medicine_master` — that list is
populated by definition, the catalogue can be empty in an environment that
never ran V19.

**3. The `pg` coexistence tests had never run** — fixed in 421c54f, and this
one was worse than "not run on this machine".

They **could not have passed anywhere**. `db/existing_schema.sql` was composed
with Davi's own adopted migrations held out, while mhn-spring's V14 and V19
both REFERENCE `drug_reference`, which Davi's V6 creates — so the file had no
loadable form and the test threw before its first assertion. Every
"coexistence is verified" line in CLAUDE.md and the README rested on that.

The premise was wrong, not just the file: production applies ONE ordered chain,
so "lay down their schema, then run Davi's chain on top" describes nothing that
happens anywhere. The dump is now the whole chain and the tests check what is
true of it.

Two things fell out on the way:

* **Seven columns come from Hibernate, not Flyway.** `insurance.from_date` and
  `to_date`, `family_connect.req_read` and three more exist in production while
  no migration creates them — and V25 builds an index on `insurance.to_date`,
  so the chain alone cannot build a loadable schema. Found by diffing the live
  database against everything the chain creates.
* **A third phantom column, the V28/V35 class again.** `period_tracking`
  `is_predicted` and `symptoms` were mapped and exist in NO environment. The
  parity guard EXEMPTED them as "ddl-auto columns the dump cannot see", which
  was false — and that exemption was the only reason it stayed green while
  `cycle_snapshot` filtered on `is_predicted`, raised UndefinedColumn on every
  call, and **returned an empty cycle history in production**. The test that
  covered it passed only against the sqlite schema the ORM built from its own
  wrong model. `DDL_AUTO_COLUMNS` is now empty: an exemption in a guard like
  that is a claim about the world and needs checking against it.

**4. The junk `HDL/LDL Ratio` row** (`ideal = 499.7, high_warn = 999`) — it is
mhn-spring's data and still wants fixing at source, but Davi now defends
itself. `_plausible` rejects a band that shares no values with Davi's own
reviewed range and falls back to the constants. Verified against the live
catalogue: all eleven reachable metrics overlap, so nothing real is rejected.

**Still yours, deliberately:** `lifestyle_log` rows Davi wrote before the unit
fix carry `quantity` in glasses. `scripts/audit_lifestyle_units.py` reports
them and converts water only — alcohol records no drink, so a "glass" could be
wine or beer and converting it would invent the reader's evening. It writes
only with `--repair --yes`, because that table is mhn-spring's.

---

## D26 — Reader-scoped data coverage: what the chat still cannot see

Prompted by "chat should be able to pull anything from the db regarding that
specific user". Rather than patch another phrasing, I enumerated every
user-scoped table in production (60 of them) and checked three layers:
does Davi map it, does anything read it, and can a chat turn reach that reader
on BOTH engines.

**Layer 3 (routing) is clean.** Every handler in `data_handlers.py` is reachable
from both engines — either from a tool executor or from the shared prologue
above the engine branch. The bypass class did not recur here. One dead symbol:
`handle_medication_command` has no caller in `app/`, kept alive only by
`tests/test_medications.py`; `handle_medication_turn` superseded it.

**Layer 2 (readers) is clean for what is mapped.** Of 31 mapped external
tables, 30 are read by chat-facing code (`family_file_access` is the exception,
and it is consulted through `file_access_exclusions` instead).

**Layer 1 (mapping) is where the gap was.** Fixed in this pass:

* `user_thp_series` — mhn-spring's V31 materialised biomarker feed, the source
  `GET /files/biomarkers` and therefore the mobile graphs use. Davi was
  re-deriving lab history by walking the newest 20 `reports` and grouping on
  the raw printed test name, so the chat's trend could disagree with the app's
  graph twice over: truncated history, and "HbA1c"/"HBA1C" counted as two
  parameters where upstream counts one. Now read first, per-document walk kept
  as the fallback for when the scheduled ingester is behind.
* `lifestyle_limit`, `body_measurement_goal`, `sahha_goal` — the targets the
  reader set in the app. Nothing read any of them.

**Still open, and deliberately not built:**

* ~~`symptom_logs` has no producer.~~ **DECIDED — build it.** "we have to
  record the symptoms reported by the user... whether its active or inactive as
  well". Done: `open_or_touch` now writes BOTH tables, so no caller can record
  the episode without the history. Retention is
  `symptom_retention_days = 400`, matching the receipt window rather than the
  180-day transcript one, because "have I had this before?" is a question about
  months and seasons; the rows are small and coarse, so a longer window costs
  little. No migration — the table has existed since V6.
* `sahha_score` (wellbeing / sleep / activity scores), `sleep_sessions` (per-
  session detail and stages) — the daily rollups Davi already reads cover the
  headline numbers, so these are additive rather than missing. Worth doing if
  readers ask about their scores by name.
* `medicine_dose_log` is NOT a gap: `app/medicines/adherence.py` deliberately
  asks mhn-spring for adherence instead of computing it, because their window
  and timezone rules are not the obvious ones. Same principle the
  `user_thp_series` change follows — agree with the app the reader is holding.


---

## D27 — A second symptom source the chat cannot see

Found by the `mhn-android-4b` session, not by my audit, and it is the kind of
miss that audit existed to catch: I listed `period_day_log` among the unmapped
user-scoped tables but did not notice it carries `symptoms text[]`.

So there are two places a reader's symptoms live:

* `symptom_logs` — what they told the CHAT. Now written (D26).
* `period_day_log.symptoms` — codes ticked in cycle tracking, written by
  mhn-spring since their V5, never read here. Independent of bleeding: a day
  with no flow and three symptoms is the ordinary mid-cycle entry.

The health summary's symptom section is window-scoped, so an empty one says
"Nothing logged in the <period> for: symptoms you reported" and never asserts
an absence it did not check — the wording is not wrong. But a reader who logs
symptoms only in cycle tracking sees nothing of them here.

Not built, deliberately: the other session was mid-flight in
`app/patterns/service.py` in the SAME working tree, and two sessions editing
one file in one checkout is how work gets lost. Proposed to them that their
`_symptoms_on(db, user_id, day)` grow a range-scoped sibling, which the summary
can then call.

One thing to solve once, wherever the merge lands: the codes are mhn-spring's
`PeriodSymptom` enum (`lower_back_pain`) while `symptom_logs.symptom` holds the
phrase the reader typed (`lower back pain`). Without a de-dup on normalised
form the reader sees both spellings of one complaint side by side.

---

## D28 — Two opposite assumptions about the tracking zone, in one codebase

Surfaced while wiring D27. Not a bug I could fix on my own judgement, because
both sides are deliberate and documented, and they now disagree.

**Side A — `app/coredata/service.py::calendar_window`** anchors on the UTC date
and says why: *"that zone is empty by default in mhn-spring and unrecoverable
from the data, so within a few hours of midnight a window can be one day out.
Same anchor `handle_correlation_query` already uses."* A known one-day risk,
accepted knowingly.

**Side B — `app/patterns/service.py::tracking_today`** (added by the parallel
session) anchors on a fixed UTC+5:30 and says why: `log_date` and the lifestyle
rollups store the resolved calendar day *at write time*, so reading them
against the UTC day is wrong for the five and a half hours before midnight UTC.
Demonstrated, not theorised — it dropped a symptom ticked today, and their own
test fixtures had the same bug (`_at(20)` built 20:00 UTC, which is 01:30 IST
the next morning, so a test asserting "yesterday evening" wrote into the
following day and passed for the wrong reason).

**The schema supports side B for the day-bucketed tables.** `db/existing_schema.sql`
sets `tracking_zone text := 'Asia/Kolkata'` in its backfills, and the V-block on
per-user timezone is explicit: *`app.tracking.zone` is global BY DESIGN ... the
lifestyle rollup tables store the resolved calendar day at write time. It
cannot be made per-user without silently reinterpreting rows already written.*
`user.timezone` exists but its own COMMENT says it is for notification
scheduling and *"must not be used to bucket lifestyle rollups"*.

**What I cannot verify from here** is side A's factual claim — whether the
`app.tracking.zone` PROPERTY is actually set in the deployed mhn-spring. The
migrations hardcode Asia/Kolkata in their own DO blocks, but that is the
backfill's local variable, not the runtime property, and this checkout of
mhn-spring has no `src/`. If the property is genuinely unset, side B's fixed
+05:30 is wrong in the other direction for every deployment that is not India.

**What I did, and did not do.** `period_day_log.log_date` reads through
`tracking_today()`, because that column is unambiguously a Spring-written
calendar day and the bug was measured. I did NOT flip `calendar_window`,
`handle_correlation_query`, or my own `targets()` (`effective_from`, same
shape, same 5.5-hour window where a goal set today would not show). Flipping
those silently would overturn a documented decision on a teammate's evidence
about a different table.

**The call needed:** confirm whether `app.tracking.zone` is set in production.
If it is, one helper should anchor every day-bucketed read and side A's comment
is stale. If it is not, side B needs to stop hardcoding +05:30. Either way it
should be one answer, not two.

(Also worth knowing, from the same session: `ZoneInfo("Asia/Kolkata")` RAISES
on Windows — no IANA database without the `tzdata` package — which is why the
helper uses a fixed offset. India has never observed DST, so the offset is
exact rather than an approximation, but that is only true for this one zone.)


### D28 — RESOLVED as a question, OPEN as a production action

The parallel session can read mhn-spring's source and its live logs, and the
decisive fact goes AGAINST the anchor they had just added. Holding the sweep
was correct.

**`app.tracking.zone` is not set.** `application.properties:88` is
`app.tracking.zone=${TRACKING_ZONE:}` — empty default — and the deployed
service warns about it on startup: *"app.tracking.zone is not set; falling back
to the JVM default (Etc/UTC). The rollup tables record which calendar day each
entry belongs to at write time, so a deployment whose zone differs from this
one will disagree about day boundaries on rows already written."*

So `calendar_window`'s docstring was right and its UTC anchor is right. Nothing
of mine needed changing.

**There is no single anchor. There are three classes:**

| what | anchor |
|---|---|
| reader-supplied calendar dates (`period_day_log.log_date`, sent by the client as a `@PathVariable`) | the reader's own zone |
| reader-facing timestamps (`symptom_logs.created_at`) | the reader's zone, bounded by instants |
| server-resolved day buckets (`lifestyle_daily_total`, `sahha_daily_total`, everything `calendar_window` reads) | `app.tracking.zone`, which is UTC today |

Our `ticked_between` use is on the first, so it stands. Their rollup reads go
back to a UTC anchor.

**The action, and it is one environment variable.** Setting
`TRACKING_ZONE=Asia/Kolkata` on the Spring service collapses all three into one
and makes the class disappear. Not ours to set.

**Why it is not hypothetical.** V1's and V35's backfills hardcode
`tracking_zone := 'Asia/Kolkata'` in their DO blocks, while everything written
since is bucketed in UTC — the same column, two anchors, 5.5 hours apart. The
disagreement the startup warning predicts is already in the data.

**Proportionate impact on the correlations engine**, which reads
`bucket_start` from both rollups: a correlation window is 28 days
(`WINDOW_DAYS`), and every row written since the backfills is UTC-bucketed, so
a window that does not straddle a backfill date is internally consistent and
today's correlations are unaffected. The risk is a window spanning a backfill,
and any comparison of pre- and post-backfill history. Not a reason to distrust
current output; a reason to pin the variable before anyone reads further back.

### D28 — two corrections to the entry above

**1. `targets()` needs no change, and the schema settles it without reading
mhn-spring.** `effective_from` is class THREE (server-resolved), not class one.
V2 on `lifestyle_limit` and V38 on `body_measurement_goal` use the same wording:

> `effective_from`  the first day the row applies to, in the tracking zone
> (`app.tracking.zone`) — the same zone `lifestyle_daily_total` buckets on, so a
> limit and the total it bounds always agree about where a day begins. **Only
> ever written as "today": the application never accepts a date from the
> client**, which is what makes a past limit unreachable rather than merely
> discouraged.

So `targets()` anchoring on `utcnow().date()` is CORRECT while the property is
unset, because both sides of that guarantee are UTC today. It becomes wrong the
moment `TRACKING_ZONE` is pinned, and it is on the sweep list for that moment —
not before.

Worth noting the design guarantee that comment states: a limit and the total it
bounds must agree about where a day begins. Pinning the variable moves both
together, so the guarantee survives the change. Reading one of them in a
different zone from the other is what would break it — which is exactly the
error the parallel session made and corrected in `dd622f0`.

**2. Pinning the variable does NOT reconcile existing rows, and my earlier
framing ("one environment variable fixes the class") was incomplete.** It fixes
rows written from that moment on. Everything written between the backfills and
the pin stays UTC-bucketed in a column that would then be documented and read as
IST. So the action is two decisions, not one:

* pin `TRACKING_ZONE=Asia/Kolkata` on the Spring service, and
* decide what happens to the history in between — left as-is and knowingly 5.5
  hours out for that span, or rewritten.

Rewriting is not obviously right: `lifestyle_daily_total` is a rollup Spring
owns and rebuilds, but `bucket_start` is also what `uq_` constraints and the
correlation windows key on. Whoever pins the variable should decide this
deliberately rather than discover it later.

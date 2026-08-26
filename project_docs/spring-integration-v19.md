# Davi × mhn-spring V7–V19 — integration plan

*Written 2026-08-25 against `D:/mhn-spring-main` @ V1–V19 and Davi @ `9b19e03`.*

Labels used throughout: **VERIFIED** = I read both sides and quote the lines.
**MEASURED** = simulated against the migration's real data. **INFERRED** = a
reasonable read of the code that I could not confirm against a running database.

---

## 1. The short answer

**V18 is a pure regression on the metric-check path.** It is the only migration
in V1–V19 that ever inserts into `traditional_health_parameters` (VERIFIED:
`grep -l "INSERT INTO public.traditional_health_parameters" V*.sql` → V18 only).
Before it, the table is empty, `_match_thp` returns `None`, and Davi answers
from `app/health/ranges.py` — which is clinically correct. After it, Davi's
unanchored substring matcher latches onto the wrong parameter and **replaces
three right answers with wrong ones**. Nobody has to do anything for this to
happen; Flyway applies it.

Ranked by "breaks a patient-facing answer, silently":

| # | What breaks | Patient-facing | Silent | Sites |
|---|---|---|---|---|
| 1 | `ldl` matches **HDL/LDL Ratio** → LDL 190 reported as *"within the usual range… that's reassuring"* | yes | **yes** | `app/health/reference.py:40,93-105` × `V18:84,1492` |
| 2 | `hdl` matches **CHOL/HDL ratio** → every HDL reading routes to *"seek medical advice promptly"* | yes | no (loud false alarm) | `reference.py:41` × `V18:54,1455` |
| 3 | `hemoglobin` matches **Glycated Hemoglobin (HbA1c)** → Hb 8 g/dL (anaemia) reported *above* range, in `%` | yes | **yes** | `reference.py:39` × `V18:79,1486` |
| 4 | `scripts/ingest_drugs.py:113` `delete(DrugReference)` NULLs ~250K `medicine_master.drug_reference_id`, irreversibly | no (other team's data) | **yes** | `ingest_drugs.py:113` × `V14:163,273`, `V19:83,146` |
| 5 | Consent gate fails **open** where Spring fails **closed** on NULL read-grants | yes (privacy) | **yes** | `app/coredata/service.py:67-79` × `FileServiceImpl.java:939-948` |
| 6 | `low_danger`/`high_danger` are graph axis bounds after V18 → the danger tier collapses to warn | yes | **yes** | `reference.py:75-84` × `V18:6-7` |
| 7 | `thp_age_range.sex` unmapped → arbitrary male/female band | latent — masked by #1–#3 | yes | `app/models/coredata.py:382-401` × `V14:251` |
| 8 | `medicine_tracking.deleted_at` / `prescriptions.deleted_at` unfiltered | latent — no Java writer exists | yes | `service.py:577-583` × `V14:330,342` |
| 9 | V14 PART 4 ALTERs Davi's own `condition_registry` / `risk_rules` / `insight_templates` | no | n/a | `V14:509-551` |

**Not broken** (REFUTED, VERIFIED by reading): V19 does not touch
`drug_reference` — no `ALTER`, `DROP`, `DELETE` or `RENAME` anywhere in the file;
`V19:4` says *"drug_reference stays in place as the raw ingest target"*.
`app/drugs/service.py` works unchanged today. The version collision is already
fixed and committed. `family_connect`, `file_access_exclusions`, `relations`,
`vital_reading`, `body_measurement`, `manual_tracking`, `unclassified_files`,
`doctor*`, `insurance`, `bills` receive no ALTER/constraint/index change in
V7–V19. Davi reads no `ai_*` table directly, so V10 and V13's classification
columns are invisible to it.

---

## 2. The version collision — **done, and since adopted as V21**

> **Outcome (2026-08-25):** merged into mhn-spring's `main` as
> `V21__davi_chat_platform.sql`; the tables are in the staging database. It was
> renumbered once more first — staged here as V20, Spring added
> `V20__staff_sessions.sql`, and the guard written in this very section caught
> it. `db/` is now gitignored; Spring owns the files. Every "V20" below is the
> historical record of how it got there.

VERIFIED: `ls db/flyway/` returned exactly two files —
`V6__davi_ai_tables.sql` (adopted long ago) and `V20__davi_chat_platform.sql`.
Commit `9b19e03 fix(flyway): Davi's V7-V10 collided with mhn-spring's chain —
consolidate to V20`. Working tree clean. `tests/test_flyway_parity.py` maps all
five tables to V20 and passes (11/11), with a
`test_davi_migrations_do_not_collide_with_mhn_spring` guard.

The user asked for one file. That is what exists:

| Was | Now |
|---|---|
| `V7__davi_user_profile.sql` (`user_profiles`) | `V20__davi_chat_platform.sql` |
| `V8__davi_feedback.sql` (`turn_feedback`) | ″ |
| `V9__davi_clinician_review.sql` (`clinician_reviewers`, `insight_review_audit`) | ″ |
| `V10__davi_erasure_and_actor.sql` (`erasure_requests`, `job_runs.actor_user_id`) | ″ |

Collision re-check against every object in V1–V19 (VERIFIED): zero table, index,
constraint or type collisions. `actor_user_id` appears nowhere in Spring's chain.
V20's only foreign `ALTER` is on `job_runs`, Davi's own table from V6.

**Action — DONE.** It is in
`D:/mhn-spring-main/src/main/resources/db/migration/` as
`V21__davi_chat_platform.sql`, and merged to their `main`.

The note for next time proved itself within a day: *pick the next Davi number
against a fresh `ls` of Spring's migration directory, not `V20+1`.* Spring
burned V10→V19 in four days and took V20 one day later. The guard now does that
`ls` automatically.

---

## 3. The drug path — move to `medicine_master`, zero migrations

Nothing is broken today. The problem is ownership: VERIFIED that
`grep -rl drug_reference --include=*.java D:/mhn-spring-main/src` returns **zero
hits** — no entity, no repository, no service. Davi's `ingest_drugs.py` is its
only writer. From V19 onward every staff correction, promotion, approval,
rejection and soft-delete lands on `medicine_master`. `drug_reference` is a
frozen CSV snapshot nobody maintains, so **Davi will serve stale drug
information indefinitely, silently**.

Every column Davi needs already exists on `medicine_master` (V14 + V19), and
`medicine_master` is Flyway-owned. **This change needs no migration on either
side.**

### Column mapping

| Davi reads | `drug_reference` | `medicine_master` | Change needed |
|---|---|---|---|
| `id` | uuid | `serial4` int (`V1:132`) | PK type only; nothing compares it |
| `name` | varchar | same | — |
| `name_normalized` | ingest `_norm()`, **punctuation kept** | trigger `lower(regexp_replace(name,'[^a-zA-Z0-9]+',' ','g'))` (`V19:41`), backfilled `V14:366`, indexed `V14:283` | none — and it **fixes** hyphenated brands: `"dolo 650"` now matches `"Dolo-650 Tablet"` |
| `composition1` / `composition2` | varchar | added `V19:22-23` | — |
| `composition_normalized` | punctuation kept | punctuation kept (`V19:42-43`), indexed `V19:32` | none — the whole-word guard at `service.py:197-201` keeps working |
| `uses` | `jsonb` array | **`used_for` `text[]`** (`V19:96-99`, `:160`) | rename; still a list, comprehension unchanged |
| `side_effects` | `jsonb` array | **`text`, comma-joined** (`V19:93-95`, `:158`) | **must split on `", "`** — else `service.py:233` iterates *characters* |
| `habit_forming` | varchar `'yes'`/`'no'` | **tri-state `boolean`** (`V19:28`) | **`.strip().lower()` raises AttributeError on a bool** → `is True` / `is False`, NULL stays silent |
| `is_discontinued` | bool | `NOT NULL DEFAULT false` (`V19:28`) | — |
| `substitutes` | jsonb array | **absent, deliberately** (`V19:9-10`) | see below |
| — | — | `status`, `deleted_at`, `merged_into_id` | **add liveness predicate** |

### Substitutes — answered

`V19:9-10`: *"substitutes (derivable: same `composition_normalized` = a
substitute — a static list would rot)"*, and `V19:32-33` creates
`idx_medicine_master_composition` for exactly that lookup. Davi has an intent
that depends on it (`_DRUG_QUERY_PATTERNS`, `app/drugs/service.py:34-39` matches
"substitutes for X" / "alternatives for X"), so dropping the line is a visible
regression.

Replace with one indexed query (~8 lines):

```sql
SELECT name FROM medicine_master
WHERE composition_normalized = :cn AND id <> :id
  AND is_discontinued = false
  AND deleted_at IS NULL AND status NOT IN ('rejected','merged','archived')
ORDER BY length(name), lower(name) LIMIT 5
```

`build_drug_reply` is pure and synchronous, so the shape is a new
`async def find_substitutes(db, drug)` plus a `substitutes: Sequence[str] = ()`
parameter, filled by its two callers (`app/chat/orchestrator.py:593`,
`app/chat/tools/executors.py:170`). This is *better* than the CSV list —
always current, and derived from composition rather than an opaque column.

### The liveness predicate — copy Spring's, not its entity

V19 uses the same filter in both directions (`V19:103-104` enrich,
`V19:138-140` import):

```sql
deleted_at IS NULL AND status NOT IN ('rejected','merged','archived')
```

VERIFIED divergence to flag to the Spring team, **not** to copy:
`MedicineMasterRepository.java:49-63 search()` filters on neither `status` nor
`deleted_at`, and `MedicineMaster.java` is still the V1 entity mapping none of
the V14/V19 columns. Their own search will surface archived and soft-deleted
rows. Follow the migration, not the entity.

### The diff

1. `app/models/coredata.py` — partial read-only `MedicineMaster` (id, name,
   name_normalized, composition1/2, composition_normalized, used_for,
   side_effects, habit_forming, is_discontinued, status, deleted_at); add
   `"medicine_master"` to `COREDATA_TABLES` so Alembic ignores it. Reuse the
   `sa.ARRAY(sa.String).with_variant(sa.JSON(), "sqlite")` pattern at
   `coredata.py:377-379` for `used_for` and `_pg_enum` at `:23-32` for `status`
   (`reference_status_enum`: draft/pending/approved/rejected/archived/merged).
2. `app/drugs/service.py` — swap the model in the four `find_drug` queries
   (`:141,154,176,189`), add the liveness predicate to each. Same index
   coverage, no perf change.
3. The three field fixes above + `find_substitutes`.
4. `app/chat/orchestrator.py:333` and `:605` — provenance `"drug_reference"` →
   `"medicine_master"` so receipts stay honest.

**Leave alone:** `_first_active` (`service.py:130-136`) sorts by
`(is_discontinued, len(name), name.lower())` — VERIFIED identical to V19's
`ORDER BY norm, is_discontinued, length(name), lower(name)` (`V19:80,:119`).
Davi already matches the team's canonicality rule. Also keep
`app/models/knowledge.py:DrugReference` and `ingest_drugs.py`: `drug_reference`
is still the ingest target V19 reads from, it just stops being what the chat
reads.

Bulk of the diff is `tests/test_drug_service.py`, which seeds `DrugReference`
rows in ~30 places.

### If `drug_reference` were ever empty (VERIFIED trace)

No crash. `find_drug` returns `None`; `orchestrator.py:583-611` has **no else
branch**, so execution falls through to the RAG/LLM path — *"side effects of
dolo 650"* silently becomes an LLM answer, breaking the "drug path is
deterministic, never the LLM" invariant with no error and no metric. The
agentic `lookup_medicine` tool returns `None` and the model answers from its
weights. The interaction refusal is unaffected (Task 25 decoupled it from the
DB hit). Worth a counter regardless of the migration.

---

## 4. Anything else broken

### 4.1 `_match_thp` picks the wrong parameter (findings 1–3)

VERIFIED code (`app/health/reference.py:93-105`):

```python
rows = (await db.execute(select(TraditionalHealthParameter))).scalars().all()  # no ORDER BY, no filter
...
if hint in hay:                        # unanchored substring
    if best is None or rank < best[0]: # strict < → first row seen wins
```

MEASURED against V18's real 193-row catalogue:

| Hint | Rank-0 collisions (insert order) | Winner today | Band it applies |
|---|---|---|---|
| `"ldl"` | HDL/LDL Ratio (`:84`, **draft**), LDL Cholesterol (`:99`), LDL/HDL ratio (`:100`), VLDL Cholesterol (`:206`) | **HDL/LDL Ratio** | `low_warn 0.4, high_warn 999.0` (`V18:1492`) |
| `"hdl"` | CHOL/HDL ratio (`:54`), HDL Cholesterol (`:83`), HDL/LDL Ratio (`:84`), LDL/HDL ratio (`:100`), Non HDL (`:116`), Trig/HDL (`:176`) | **CHOL/HDL ratio** | `low_warn 3.0, high_warn 5.0, high_danger 8.4` (`V18:1455`) |
| `"hemoglobin"` | Glycated Hemoglobin (HbA1c) (`:79`), Hemoglobin (`:86`) | **HbA1c** | `low_warn 4.0, high_warn 5.7, high_danger 16.1`, unit `%` (`V18:1486`) |

Failure scenarios, traced through `_classify_bands` (`reference.py:75-84`) and
`_backend_reply` (`app/chat/data_handlers.py:150-170`):

- *"my LDL is 190"* (statin territory) → `190 < 999.0` → `("normal","")` →
  **"An LDL/HDL Ratio of 190 ratio is within the usual range for your age
  (0.4–999). That's reassuring — keep monitoring as your doctor advises."**
  Pre-V18: *"above the typical range… consult your doctor."* Active
  reassurance on a dangerous value, from a row Spring marked `draft`.
- *"my HDL is 45"* (good HDL) → `45 >= 8.4` → `("danger","high")` →
  **"…well above the usual range for your age (3–5). Please seek medical advice
  promptly — contact your doctor or urgent care."** Every plausible HDL fires
  this. A permanent false alarm on the safety-escalation path.
- *"my hemoglobin is 8"* (real anaemia) → `8 < 16.1` → `("warn","high")` →
  **"…is above the usual range for your age (4–5.7 %)"**. Direction inverted,
  wrong unit, wrong parameter name.

The backend wins unconditionally over Davi's own constants
(`app/chat/data_handlers.py:269-271`: `if backend is not None: return backend`),
so there is no safety net.

**Why a word-boundary regex is NOT enough:** `"hdl"` is already a whole word in
`"CHOL/HDL ratio"`. A status filter is also not enough: only HDL/LDL Ratio is
`draft`; CHOL/HDL ratio, LDL/HDL ratio and VLDL Cholesterol are all `approved`
(VERIFIED, `V18:54,100,206`).

**The fix that actually works, and is smaller:** replace `_THP_HINTS` substrings
with a curated `metric_key → exact THP name` map, matched on
`lower(name) == key`. Names verified present in V18:

| metric key | THP name |
|---|---|
| `blood_sugar`, `fasting_glucose` | `Fasting Blood Sugar` (`V18:71`) |
| `random_glucose` | `Random Blood Glucose` (`V18:129`) |
| `hba1c` | `Glycated Hemoglobin (HbA1c)` (`V18:79`) |
| `hemoglobin` | `Hemoglobin` (`V18:86`) |
| `ldl` | `LDL Cholesterol` (`V18:99`) |
| `hdl` | `HDL Cholesterol` (`V18:83`) |
| `total_cholesterol` | `Total Cholesterol` (`V18:169`) |
| `heart_rate`, `spo2`, `bmi` | absent from V18's lab catalogue — leave unmapped |

An unmapped key returns `None`, which falls back to `app/health/ranges.py` —
i.e. the correct pre-V18 behaviour. Add `WHERE status='approved' AND visible
AND deleted_at IS NULL` and `.limit(1)` to the same query; that also fixes 4.3
and 4.4 below, and stops loading 193 rows on every reply. A curated map is also
what `V14:232` was built for (unique `code` column) if this is ever formalised.

### 4.2 `ingest_drugs.py` destroys the other team's catalogue lineage

VERIFIED both sides. `scripts/ingest_drugs.py:113` `await
db.execute(delete(DrugReference))`; docstring `:5` — *"Truncate-and-reload
semantics (the CSV is the source of truth)"*. `V14:273` (and `:163` on
`prescription_item`) `drug_reference_id uuid REFERENCES drug_reference(id) **ON
DELETE SET NULL**`; `V19:83` and `V19:146` populate it for effectively every
one of ~250K rows.

**Scenario:** someone follows `docs/production_integration.md:224` and re-runs
the documented ingest against the shared `DATABASE_URL`. The DELETE succeeds.
Postgres fires `ON DELETE SET NULL` — a DB-level action, invisible to the ORM —
and NULLs `drug_reference_id` across all of `medicine_master` and every
`prescription_item`. No error, no warning, no log line. It is unrecoverable:
`DrugReference` uses `UUIDPrimaryKey` (`app/models/common.py:55-57`) so reloaded
rows get fresh uuids, and V19's relink is guarded by `AND m.drug_reference_id IS
DISTINCT FROM ref.id` (`V19:105`) on a migration Flyway will never re-run.

**Laziest gate that works:** refuse to run when `medicine_master` has any
non-NULL `drug_reference_id`, unless `--i-know` is passed. Three lines. A proper
upsert keyed on `name_normalized` is the real fix and can wait until someone
actually needs to reload the CSV.

### 4.3 The consent gate fails open where Spring fails closed

VERIFIED: `grep -n "req_read" V*.sql` across V1–V19 → **zero hits**.
`V1:339-353` `family_connect` has only `req_file_share bool DEFAULT true NOT
NULL` and `acc_file_share bool NULL`. The columns exist in production only
because `spring.jpa.hibernate.ddl-auto=update` (`application.properties:14`)
made them from `FamilyConnect.java:43-53`.

Spring (`FileServiceImpl.java:939-948`): `Boolean.TRUE.equals(c.getReqRead())` →
NULL means **deny**. Davi (`app/coredata/service.py:67-79`): falls back to
`req_file_share`, `NOT NULL DEFAULT true` → **grant**. The fallback can only
ever resolve permissive. It is pinned by
`tests/test_prod_adaptation.py:191-194::test_legacy_fallback_when_new_columns_null`.

**Scenario:** a connection whose `acc_read` is NULL. Davi's chat lists and
summarises the owner's documents; Spring's own `GET /files/{type}/{id}/url`
would 403 the same request. A consent gate more permissive than the system of
record.

Two corrections to the briefing: the entity is
`@Column(name="req_read", nullable=false) private Boolean reqRead = true;`, so
whether legacy rows are actually NULL depends on how ddl-auto materialised a
NOT-NULL column on a populated table — **not verifiable without querying the
DB**. And "every family read raises `UndefinedColumn` on a Flyway-built DB" is
correct in principle (Davi's `select(FamilyConnect)` names both columns, and the
orchestrator's SAVEPOINTs at `app/chat/orchestrator.py:474-477,553` turn it into
a quiet *"I couldn't find a connected family member"*) but bites CI/DR only, not
production.

**Two actions:** (a) drop the fallback to match `Boolean.TRUE.equals`, invert
that test; (b) ask the Spring team for a `V2x` that
`ADD COLUMN IF NOT EXISTS req_read/acc_read/req_write/acc_write` so Flyway owns
what both apps depend on.

### 4.4 The danger tier collapsed into warn

VERIFIED. `V18:6-7` states it: *"Danger bounds are pinned to the graph bounds
(the app's warning-only model)"*, and every seeded row bears it out —
`low_danger == min` and `high_danger == max` throughout. LDL Cholesterol
(`V18:1524`): `high_warn 100.0, high_danger 228.0, max 228.0`. Total Cholesterol
(`V18:1635`): `high_warn 200, high_danger 288, max 288`.

`_classify_bands` (`reference.py:82-84`) returns `danger` only past
`high_danger`. **Scenario:** total cholesterol 260 mg/dL → `warn`, not `danger`
→ *"consider discussing with your doctor"* instead of *"seek medical advice
promptly"*. Under-escalation, which is the wrong direction for a safety floor.

Simplest fix: stop treating `low_danger`/`high_danger` as clinical. Derive
severity from the warn bands plus Davi's own DRAFT constants in
`app/health/ranges.py`. Alternative (needs the Spring team): a separate pair of
genuinely clinical danger columns. Only manifests on parameters that match
correctly, so it is worthless to fix before 4.1.

### 4.5 `thp_age_range.sex` — real, currently unreachable

VERIFIED `V14:251` `ADD COLUMN IF NOT EXISTS sex varchar(8) NOT NULL DEFAULT
'any'` and `V14:261` unique index `(thp_id, sex, age_min, age_max)`; V18 seeds
277 ranges using it, e.g. Hemoglobin adult `('female',18,59,low_warn 12.0)` and
`('male',18,59,low_warn 13.5)` (`V18:1499-1506`). `ThpAgeRange`
(`app/models/coredata.py:382-401`) maps no `sex`; `reference.py:120-131` orders
by `age_min` alone and takes the first covering row — two rows tie at
`age_min=18` and the winner is planner order.

**Downgraded from the briefing's ranking:** of Davi's 11 hint keys only
`hemoglobin` and `hdl` touch sex-split parameters, and *both are hijacked by 4.1
before sex matters*. Fix it in the same edit as 4.1 (map `sex`, filter
`sex IN ('any', <gender>)` preferring the specific row; `User.gender` is already
mapped at `app/models/core.py:60` and `_norm_gender` exists at
`app/coredata/service.py:143`) — but it is not what is hurting patients today.

### 4.6 Aliases moved to `thp_alias`; Davi reads a NULL array

VERIFIED. `V14:355-361` migrated the `aliases` varchar[] into a new `thp_alias`
table, with the comment at `V14:356`: *"The array stays read-only until the AI
matcher switches to thp_alias (open question Q1)."* **Davi is that matcher, and
Q1 is still unanswered.** `V18:11-13` inserts all 193 parameters without
`aliases` and `V18:210` puts all 1184 aliases into `thp_alias`.
`reference.py:99-100` reads only `thp.aliases`, NULL for every new row.

Consequence: every synonym a lab actually prints — `PCV`, `HCT`, `A1c`,
`AG Ratio`, `FBS` — is invisible. Largely moot if 4.1 ships as a curated
name map, but answer Q1 in writing so Spring knows.

### 4.7 Soft-delete columns unfiltered — LATENT

VERIFIED columns (`V14:342` `medicine_tracking.deleted_at`, `V14:330`
`prescriptions.deleted_at`) and VERIFIED that Davi filters only
`stopped_at IS NULL` + `private IS FALSE` (`app/coredata/service.py:577-583`)
and `user_id` (`:404-412`).

**Downgraded:** `grep -rln "setDeletedAt" --include=*.java src/` returns **zero
hits**. Nothing in mhn-spring writes either column. Dormant until someone ships
the soft-delete. Map them cheaply when convenient; they cause no wrong answers
today.

### 4.8 V14 PART 4 ALTERs Davi's own tables — governance, not breakage

VERIFIED `V14:509-551` ALTERs `condition_registry` (+`kind`, `icd10_code`,
`category`, `description`, `is_hereditary`, `status`, `merged_into_code`,
maker-checker columns), `risk_rules` and `insight_templates`. Header at
`V14:16-18`: *"if the AI team ports this into their Alembic chain instead,
delete PART 4 before running."* Nobody did.

All are `ADD COLUMN IF NOT EXISTS` with defaults, and Davi's whole-entity
selects name only its own mapped columns, so **reads and writes both keep
working**. The real issue is two truths for one fact:
`condition_registry.status` (`V14:515`, default `'approved'`) vs Davi's
`where(ConditionRegistry.active.is_(True))` (`app/knowledge/registry.py:251`).
A condition the dashboard archives or merges stays active in Davi's keyword
index. `tests/test_flyway_parity.py` cannot see this — PART 4 lives in someone
else's file.

Adopt-or-delete decision, not an outage. Note `condition_registry.is_hereditary`
+ `category` are directly useful to the insights engine's family-history
patterns if adopted deliberately.

### 4.9 Four PG enums are hand-copied and would raise on any `ADD VALUE`

VERIFIED currently correct: `app/models/coredata.py:23-32` binds
`vital_type_enum`, `body_measurement_type_enum`, `lifestyle_log_type_enum`,
`manual_tracking_type_enum` with `create_type=False`, and all four match `V1`
exactly. The only `ALTER TYPE` statements in the whole chain are `V1:1064` and
`V7:24-25`, neither of which Davi binds. MEASURED against the installed
SQLAlchemy 2.0.52: an ENUM result processor raises `LookupError` on **fetch**,
not bind — so one future `ALTER TYPE lifestyle_log_type_enum ADD VALUE 'juice'`
breaks reads for every user with such a row, from a migration touching no table
Davi maps. Zero test coverage (`conftest.py:32-46` degrades `_pg_enum` to
`String(32)` on sqlite). Cheapest fix: bind plain `String` on the read-only
columns; only `lifestyle_log` needs the real cast for its one INSERT.

### 4.10 Test coverage of all of the above: zero, by construction

VERIFIED. `tests/conftest.py:46` and `:138` both call
`Base.metadata.create_all` — the test schema is built **from Davi's own partial
mappings**, on sqlite *and* on Postgres. `sex`, `status`, `visible`,
`deleted_at`, `thp_alias` do not exist in any test database. The assumption
under test defines the fixture. `tests/test_ranges.py:172-186` seeds exactly one
parameter with helpful aliases and one range, so
`test_backend_graduated_bands` passes and proves nothing.
`db/existing_schema.sql` is still the V1 baseline (its `thp_age_range` has no
`sex`; `report_parameter_value`, `drink_master`, `thp_alias`,
`prescription_item`, `mood_log`, `employee`, `role` are absent entirely), so the
coexistence check has never seen V7–V19. **That is why none of this was caught.**

---

## 5. Newly available — for the per-user memory document

Ranked by value. Consent implications called out per row; the four-condition
gate means *accepted connection + owner-side `req_read`/`acc_read` + not
`private` + no `file_access_exclusions` row*, which Davi already implements in
`can_view_document` (`app/coredata/service.py:340-378`).

| Rank | Source | What it gives | Consent |
|---|---|---|---|
| 1 | **`medical_condition`** (V7 + V14) | Structured conditions / surgeries / allergies per user, replacing self-reported chat capture (`app/chat/profile.py:35-46`). Only structured source of **medication allergies** (`category='medication'` + `severity` + `reaction`) — and `build_drug_reply` currently consults no allergy list at all. | Same gate, unchanged — `medical_condition` is a `resource_type_enum` member (`ResourceType.java:5`) and `FamilyServiceImpl.java:635-641` shares it identically. Add `"condition": "medical_condition"` to `_RESOURCE_TYPE` (`service.py:296-303`) and the existing gate covers it. |
| 2 | **`mood_log`** (V11) | One row per user per day: `score` 1–10 + `factors text[]`, indexed `(user_id, log_date DESC)`. Live: `MoodController`, `MoodServiceImpl`. A 30-day average + top factors answers *"how have I been lately"* directly. | **None needed.** No `private` column and `mood` is not a `resource_type_enum` value — no code path can family-share it. Cheapest clean addition here. |
| 3 | **`reports.name` / `.date`** etc. (V13) | User-typed name and the date printed on the document, on all six section tables. Davi still sorts and titles by `created_at` (the moment mhn-ai *filed* the row) and `content.ai.classification.title` (`service.py:306-317,404-412`), so it says *"your lipid profile from 3 Sep"* when the blood was drawn 12 Aug. | Same gate — same tables. |
| 4 | **`period_status.pregnancy`** (V5 + V9) | Authoritative pregnancy state + `predictions_suppressed`, beating Davi's self-reported `user_profiles.is_pregnant`. | **The exception.** `period_settings.private` is `DEFAULT TRUE` (inverted from every other table) and `V5:363-372` says `resource_type_enum` was deliberately *not* extended, because the family model is default-ALLOW. So `can_view_document` **has nothing to gate on and will never say no**. Own-data only; honour `period_settings.enabled`; leave `contraception`, `diagnosed_pcos`, cycle dates and `period_day_log` out of the prompt entirely. `per-user-memory.md §5.1` should state explicitly that **the absence of a gate is not permission**. |
| 5 | `medicine_tracking.effective_end` (V16:120-155) | Now trustworthy (NULL = genuinely indefinite; the V1 version turned an indefinite course finite the moment `extended_till` was set). Partial index `idx_medicine_tracking_user_active` built for exactly this predicate. Davi's `active_medications` never looks at it, so a course that ended last month is still "current". | Same gate. One extra WHERE. |
| 6 | `medical_record_medicine` (V7:52-66) | Links a tracked medicine to the condition it treats — *"metformin 1000mg (for type 2 diabetes)"* from the record rather than model inference. | As #1. Only worth it after `medical_condition` is mapped. |
| 7 | `report_parameter_value` (V14:181-206) | Exactly the shape `own_labs` + `trends` need: one row per extracted parameter, `thp_id`, `value_numeric`, `zone`, `measured_on`, indexed `(user_id, thp_id, measured_on DESC)`. | Carries no `private` flag — only `section` + `record_id`. Any family-facing use must re-gate through `can_view_document(section, record_id)`. **VERIFIED: no writer exists** — `grep -rl report_parameter_value --include=*.java` returns nothing in mhn-spring *or* mhn-ai. Build with a fallback, never as sole source. |

Two supporting notes:

- **Davi's current lab read makes trends structurally impossible.**
  `recent_lab_values` (`service.py:718-763`) dedupes with `if key in seen:
  continue`, keeping only the most recent occurrence of each parameter name. The
  memory doc's `trends: [{metric, from, to, since, direction}]` cannot be built
  from it. Either #7 gets a writer, or trends need their own query keeping N
  points per name. Say so in the memory doc — the JSON currently implies the
  data is already there.
- **V17's drink catalogue is real but empty for us.** 282 curated drinks with
  `caffeine_mg_per_serving` and `standard_units_per_serving`, and
  `lifestyle_log.drink_id`/`caffeine_mg`/`alcohol_units` columns (`V14:310-315`)
  — but `grep -rl drink_master --include=*.java` returns nothing, so they are
  NULL. `lifestyle_totals` summing raw `quantity` means a 30 ml whisky and a
  330 ml beer count the same. Future source, not a current one. Note Davi is
  itself a *writer* to `lifestyle_log` (`add_lifestyle_log`, `:524`) and would
  be the one leaving those columns NULL if the tracker ever fills them.
- **Nothing for Davi in V12 / V15 / V14's staff tables** (`employee`, `role`,
  `approval_request`, `employee_activity_log`) — no `user_id`, no patient data.
  Flag only that two staff-identity models now head for the same database:
  Spring's `employee` + `role` vs Davi's `clinician_reviewers` (V20). Decide
  that on purpose, not by whichever ships first.

---

## 6. What to do, in order

| # | Do | Why this order |
|---|---|---|
| 1 | **Gate `scripts/ingest_drugs.py:113`** (3 lines: refuse if any `medicine_master.drug_reference_id IS NOT NULL`, override flag to bypass) | It damages *another team's* data, irreversibly, on a command Davi's own docs tell you to run. Blast radius outside our repo, and a one-command mistake. Cheapest thing on the list. |
| 2 | **Rewrite `_match_thp` as a curated name map** + `status='approved' AND visible AND deleted_at IS NULL` + `.limit(1)`, and map `ThpAgeRange.sex` in the same edit | One function is actively reassuring patients about dangerous LDL values and routing every normal HDL to urgent care. Fixes 4.1, the draft-row exposure, 4.5 and 4.6 in one diff. Do it before 4.4 — the danger-tier fix is moot while the wrong parameter is being matched. |
| 3 | ~~**Hand the migration to the Spring team**~~ **DONE** — adopted as `V21__davi_chat_platform.sql` | Zero engineering left; it is pure coordination latency, and Spring is burning version numbers fast. Start it early, it runs in parallel with everything else. |
| 4 | **Refresh `db/existing_schema.sql`** from a V1–V19 database | The single cheapest change that makes the rest testable, and the only reason none of this was caught. Do it before writing any new partial mapping, or the mapping is untested against production shape (CLAUDE.md's own gotcha). |
| 5 | **Add one pg-marked test** that loads Spring's real V1–V19 chain and asserts, per mapped model, that every column exists with a compatible type | One test replaces 21 hand-written table tests and would have caught six of these findings. Depends on #4. |
| 6 | **Drop the consent fallback** at `service.py:67-79` to `Boolean.TRUE.equals` semantics; invert `test_prod_adaptation.py:191-194`. Ask Spring for a `V2x` adding the four grant columns to Flyway | Privacy-relevant and permanently wrong-direction, but it needs a decision with the Spring team about legacy rows, so it cannot be a same-day fix. |
| 7 | **Move the drug path to `medicine_master`** (§3) | No migration, no coordination, but a real diff with ~30 test fixtures. Nothing is broken today — this buys freshness and staff curation, not a fix. |
| 8 | **Fix the danger-tier collapse** (4.4) | Only manifests on correctly-matched parameters, so it is worthless before #2 and needs a clinical decision (derive from warn bands, or ask Spring for real danger columns). |
| 9 | **Decide V14 PART 4: adopt or delete** (4.8), and answer Spring's open question Q1 in writing (4.6) | Governance. Two chains write the same three tables and neither knows about the other — but reads and writes both work, so nothing is on fire. |
| 10 | **Map the new sources** (§5), in the ranked order | Feature work. Depends on #4 for a trustworthy coexistence check. |

Items 4.7 (soft-delete columns) and 4.9 (hand-copied enums) are deliberately
unscheduled: both are latent, both are one-line fixes, do them opportunistically
whenever the surrounding file is already open.

---

## 7. What I could not verify, and why

| Claim | Why not | Consequence if wrong |
|---|---|---|
| **Whether V18 has actually been applied to the shared database** | No DB access from this machine; I read migrations only. | If V18 is not yet applied, findings 1–3 and 4.4 are *pending*, not live — but Flyway will apply it, so the fix is needed either way. Check `SELECT count(*) FROM traditional_health_parameters`. |
| **Whether `family_connect.req_read`/`acc_read` are NULL on legacy rows** | Depends on how `ddl-auto=update` materialised a `nullable=false` column with a Java-side default on an already-populated table — not determinable from source. | If ddl-auto backfilled `true`, Davi and Spring agree today and 4.3 is latent rather than live. The fallback direction is still wrong. Check `SELECT count(*) FROM family_connect WHERE req_read IS NULL OR acc_read IS NULL`. |
| **Whether production `drug_reference` actually holds the ~250K rows** | `V19:13` explicitly hedges (*"On a database whose drug_reference is empty this only adds the columns"*). INFERRED yes, from `MedicineServiceImpl.java:70-76` complaining that *"the drug catalogue now being imported has no dosage-form column at all"*, which only makes sense if rows moved. | If empty, §4.2's blast radius is zero and the drug path is already silently on the LLM (§3's fallthrough trace). Check `SELECT count(*) FROM drug_reference` and `SELECT count(*) FROM medicine_master WHERE drug_reference_id IS NOT NULL`. |
| **The exact physical row order of an unordered `SELECT` on `traditional_health_parameters`** | Insert order is the physical order on a freshly loaded, never-updated table, so *which* wrong row wins in 4.1 is INFERRED. The collisions themselves and the rank tie are VERIFIED. | Only changes which wrong answer you get. Every candidate is the wrong parameter with an incompatible unit. |
| **Whether the staff dashboard that owns V14's tables exists anywhere** | `mhn-spring` has no dashboard module (`modules/` = ai, auth, doctor, family, files, manualTracking, medicalHistory, medicine, mood, period, s3, user) and V14 calls itself a *"single-file deploy"* of the *"R&D dashboard schema delta"* — DDL shipped ahead of its application. | `report_parameter_value`, `thp_alias`, `prescription_item`, `medicine_unmatched`, `approval_request` may stay empty indefinitely. Do not build anything on them as a sole source. |

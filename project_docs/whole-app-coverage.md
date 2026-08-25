# Whole-app coverage — build plan

**The answer in one paragraph.** Davi maps 14 of the 33 per-user tables in the
shared production database. Of the 19 it cannot see, **six are worth mapping**,
**seven must never be read**, and **six are dead or redundant**. The six worth
mapping are `medical_condition`, `lifestyle_limit`, `medicine_dose_log`,
`mood_log`, plus two already-mapped models gaining a column each
(`medicine_tracking.deleted_at`, `user.timezone`); the cycle tables are a
deliberate maybe (§8 Q4). Two of the tables the earlier session ranked highest —
`report_parameter_value` and `sleep_sessions` — **have no writer in either
sibling repo** and would ship as features that always return nothing. Everything
here is READ-ONLY over tables that already exist, so there is **no Flyway
migration, no mhn-spring change and no mhn-ai change**. Four independent bugs
found on the way out are Stage 0 and ship before any of it.

All claims below are labelled VERIFIED (read in the code, cited file:line) or
INFERRED. Where the design and the review disagreed, §9 records which won.

---

## 1. What Davi can and cannot see today

63 tables in production, 33 per-user. Davi maps 14 of those 33
(`COREDATA_TABLES`, `app/models/coredata.py:36-56` — VERIFIED by reading the set;
it lists 21 names, the remainder being reference and consent tables).

### The 19 unmapped, ranked by what the user actually asked for

| # | Table | The ask it serves | Has a writer? | Verdict |
|---|---|---|---|---|
| 1 | `medical_condition` | "medical history" — conditions, surgeries **and allergies** | YES (`MedicalHistoryRepository.java:23`) | **MAP — Stage 1** |
| 2 | `lifestyle_limit` | "how much water I drank" — the *target* half | YES (`LifestyleLimitRepository.java:52`) | **MAP — Stage 1** |
| 3 | `medicine_dose_log` | "medications" — did I take them | YES (`MedicineDoseLogRepository.java:37`) | **MAP — Stage 2** |
| 4 | `mood_log` | "other manual trackers" | YES (`MoodLogRepository.java:26`) | **MAP — Stage 2** |
| 5 | `period_settings` | cycle — the privacy gate itself | YES (`PeriodSettingsRepository.java:19`) | **Stage 4, gated (§8 Q4)** |
| 6 | `period_status` | pregnancy / contraception as drug-safety context | YES (`PeriodStatusRepository.java:29`) | **Stage 4, gated** |
| 7 | `period_tracking` | cycle history | YES (`PeriodTrackingRepository.java:27`) | **Stage 4, gated** |
| 8 | `period_day_log` | cycle symptom detail | YES | SKIP — behind two traps, for a question #7 mostly answers |
| 9 | `report_parameter_value` | "pull the reports" — structured lab values | **NO** | **SKIP — no writer** |
| 10 | `sleep_sessions` | "wearables data that we get" | **NO** | **SKIP — dead schema** |
| 11 | `lifestyle_daily_total` | tracker totals | YES | SKIP — `service.py:545` already computes this correctly |
| 12 | `lifestyle_weekly_total` | tracker totals | YES | SKIP — same |
| 13 | `lifestyle_monthly_total` | year-scale totals | YES | SKIP — YAGNI + calendar/rolling window mismatch |
| 14 | `refresh_token` | — | YES | **NEVER — session credential** |
| 15 | `device_token` | — | YES | **NEVER — push credential** |
| 16 | `subscriptions` | — | YES | **NEVER — billing** |
| 17 | `event_log` | — | YES | **NEVER — audit, injection surface** |
| 18 | `notification` | — | YES | **NEVER — duplicates its sources** |
| 19 | `sos_contact` | — | YES | **NEVER — third-party PII** |

**The two corrections that change the shape of the whole plan** (both VERIFIED
by exhaustive grep of `D:/mhn-spring-main/src/**` and all of `D:/mhn-ai-main`):

- **`report_parameter_value` is written by nobody.** It exists only as V14 DDL
  (`db/existing_schema.sql:2741`), five indexes, and the `v_unmatched_parameter`
  R&D view. Zero hits in Spring's Java, zero anywhere in mhn-ai. Davi keeps
  parsing `content.ai.extraction.results[]`
  (`app/coredata/service.py:687-778`). **Consequence: the "trends" memory field
  is cut** — a trend over JSON envelopes means re-parsing every document on every
  turn, which is the worst possible cost profile for a prompt-resident field.
- **`sleep_sessions` is dead.** Three grep hits in mhn-spring, all inside
  `entities/SleepSession.java:22-24`. No repository, no service, no writer. Sleep
  questions are already answered from `manual_tracking` via
  `latest_manual_metrics` (`app/coredata/service.py:607`).
- Bonus: **`medicine_tracking.adherence_rate_30d` has no writer either**
  (VERIFIED: zero hits for `adherenceRate|adherence_rate_30d`). The "one-line lazy
  adherence answer" does not exist. But Spring **does** serve adherence live — §6.

The gap that matters most: Davi today knows a reader's **family** history
(pedigree) and not their own diagnoses or allergies. That is Stage 1.

---

## 2. What to add, in priority order

House style is fixed: partial ORM mappings in `app/models/coredata.py` using
`_pg_enum(..., create_type=False)` (`app/models/coredata.py:23-32`);
frozen-dataclass read functions in `app/coredata/service.py` with a total ORDER BY
and an explicit limit; every `__tablename__` added to `COREDATA_TABLES` (`:36-56`),
which `app/models/core.py:38` merges into `EXTERNAL_TABLES` so Alembic never
proposes DDL for a Flyway-owned table. Forgetting that registration is silent
until a migration run.

### 2.1 `medical_condition` — conditions, surgeries, allergies (Stage 1)

| Column | Type | Note |
|---|---|---|
| `id` | `sa.Integer` PK | |
| `user_id` | `sa.Uuid` | |
| `name` | `String(255)` | |
| `type` | `_pg_enum("medical_record_type_enum","condition","surgery","allergy")`, **nullable, default `"condition"`** | a pre-V7 row has no type and *is* a condition |
| `status` | `_pg_enum("medical_condition_status_enum","active","resolved","chronic","monitoring","controlled","remission")`, nullable | six values: V1's four (`db/existing_schema.sql:622`) + V7's two (`:2176-2177`) |
| `category` | `_pg_enum("allergy_category_enum","food","environmental","medication")`, nullable | allergy only |
| `severity` | `_pg_enum("allergy_severity_enum","mild","medium","severe")`, nullable | allergy only |
| `reaction` | `String(255)`, nullable | |
| `condition_code` | `String(32)`, nullable | V14 taxonomy hook → `app/knowledge/registry.py` codes |
| `started_on` / `ended_on` | `DateTime(tz=True)`, nullable | V7 dropped NOT NULL — an allergy has no since-when |
| `private` | `Boolean`, nullable, default `False` | family-sharing flag |
| `deleted_at` | `DateTime(tz=True)`, nullable | V14 soft delete (`:2866`) |

**Never mapped, never selected:** `family_linked_relations` jsonb (`:2853`) — a
list of *which relatives* carry the condition. Another person's health status
living inside a row that passes a naive `user_id = me` check. Also `notes`
(unbounded clinical free text: tool result only, never a prompt block),
`episodes`, `surgery_status` (redundant — past vs upcoming *is* `started_on`
against today, `db/existing_schema.sql:2157-2160`), `hospital`, `surgeon_name`.
Use an explicit column allowlist with a test asserting it, so the next column
another team adds is not absorbed silently.

Read function:

```python
async def health_records(db, owner_id, *, kinds=("condition","surgery","allergy"),
                         viewer_id=None, limit=20) -> list[HealthRecord]:
```

Predicate, VERIFIED against Spring:

| Condition | Source |
|---|---|
| `user_id == owner_id` | `MedicalHistoryRepository.java:23` |
| `deleted_at IS NULL` — always, both paths | V14 column; Spring does not filter it yet (zero `deleted_at` hits in Spring Java), so this is a no-op today and automatically correct the day the delete flow ships |
| `type IN kinds OR type IS NULL` (when `"condition" in kinds`) | pre-V7 rows |
| **Owner path** (`viewer_id in (None, owner_id)`): **no privacy filter** | `MedicalHistoryRepository.java:23` applies no `private` predicate — the owner sees their own private rows |
| **Family path**: `private IS FALSE` (NULL **excluded**), then per row `can_view_document(db, viewer, owner, "medical_condition", row.id, is_private=row.private)` | `FamilyServiceImpl.java:636 getByUserIdAndIsPrivateFalse` + `:651-661`; `medical_condition` **is** a `resource_type_enum` value (`db/existing_schema.sql:399-401`), so the existing four-condition gate (`app/coredata/service.py:340-377`) applies unchanged — do not write a second gate |
| `ORDER BY name ASC, id ASC`, request `limit + 8` for exclusion headroom | matches `latest_documents` (`service.py:409-411`) |
| Python post-sort `key=lambda r: (r.severity != "severe", r.name)` | so a severe allergy is never the row truncated by the cap |

Spring **contradicts itself** on NULL `private`: `MedicalHistoryServiceImpl.java:490`
treats NULL as *shared*, `FamilyServiceImpl.java:636` excludes it. Follow the
family path — it is the one guarding actual cross-user reads, and erring toward
hiding is the correct tie-break.

**Delivery: the memory document (fields 2 and 3) AND `build_drug_reply`.** Not a
tool, not a legacy handler — §2.5 explains why the drug path needs its own wiring.

### 2.2 `lifestyle_limit` — the missing half of every tracker answer (Stage 1)

Six columns: `id` (BigInteger with sqlite Integer variant), `user_id`, `log_type`
(`_pg_enum("lifestyle_log_type_enum", ...)` — already bound at
`app/models/coredata.py:185-189`, reuse it verbatim), `effective_from` (Date),
`limit_value` (`Numeric(8,2)`, **nullable**). `unit` is deliberately unmapped:
reads take the unit from `DEFAULT_UNITS` (`app/coredata/service.py:515-521`),
because keying on a stored unit splits a series in two
(`db/existing_schema.sql:1244-1249`).

```python
async def lifestyle_limits(db, user_id, on=None) -> dict[str, float | None]:
```

Fetch every row with `effective_from <= on`, `ORDER BY log_type, effective_from,
id`, keep the last per type in a Python loop — a handful of rows per user, and it
avoids `DISTINCT ON`, which sqlite does not have. Matches
`LimitSchedule.java:57-70`.

**Two read rules, both stated by Spring and by the schema:**

- `limit_value IS NULL` means the reader **removed** their limit from that day on.
  A **missing key** means they never set one (`LimitSchedule.isUnset`,
  `LimitSchedule.java:73`). Neither is zero.
- Failure scenario if you get this wrong: reader deletes their 8-glass water
  limit; Davi reads NULL as 0 and tells them they are 400% over a limit they
  deliberately cleared.

**Delivery: memory document (field 8) + folded into `build_health_snapshot`'s
existing lifestyle line (`app/chat/context.py:201-207`).** No tool. "You drank 4
glasses" and "your target is 8" belong in the same sentence, and that sentence
already exists.

### 2.3 `medicine_dose_log` — adherence (Stage 2)

Mapping: `id` (BigInteger variant), `tracking_id`, `user_id`, `scheduled_date`
(Date), `slot` (`String(1)`, M/A/E/N), `status`
(`_pg_enum("dose_status_enum","pending","taken","skipped","forgotten")`),
`taken_at`, `skip_reason`, `is_prn`. Skipped: `scheduled_time`, `dose_qty`,
`created_at`.

```python
async def dose_adherence(db, user_id, *, days=30, today=None) -> list[DoseAdherence]:
```

**OWNER-ONLY, and there must never be a viewer parameter**: `medicine_dose_log` is
not in `resource_type_enum`, so no family path exists to gate.

The numbers must match Spring's live endpoint (§6), which means all five of these:

| Rule | Value | Why |
|---|---|---|
| Window length | **30 days** (`DEFAULT_ADHERENCE_DAYS = 30`, `MedicineTrackingServiceImpl.java:84` — VERIFIED) | not 14 |
| Window bounds | `from = today - (days-1)` … `today` **inclusive** (`:388`) | excluding today hides this morning's dose |
| "today" | `LocalDate.now(userZone.of(user))` (`:385`) — the **reader's own zone**: `user.timezone` (`db/existing_schema.sql:2037`) falling back to the global `app.tracking.zone` (`UserZone.java:39-60` — VERIFIED) | UTC shifts the whole window for an IST reader before 05:30 |
| PRN | `is_prn IS FALSE` (`:400`) | `uq_medicine_dose_log_slot` is partial on `is_prn = false`; an as-needed course has no denominator |
| Pending | excluded from the denominator (`:412`) | `idx_medicine_dose_log_flip_job` (`db/existing_schema.sql:733`) shows a background job flips pending → forgotten *after* the scheduled time passes |

Plus: join `medicine_tracking` on `tracking_id`, filter
`medicine_tracking.deleted_at IS NULL`. `taken = status == 'taken'`;
`missed = status IN ('skipped','forgotten')`; **`scheduled = taken + missed`,
derived from rows that exist — never from a date range**, because
`MedicineDoseLogRepository.java:74 deletePendingAfter` deletes rows for days that
will never happen, with an explicit comment that counting them would dent
adherence. `forgotten` is machine-set: never narrate it as "you skipped".

Failure scenario if the window is wrong: an Asia/Kolkata reader takes their 09:00
metformin and asks at 10:00 IST. The MHN app says **92.3% (12 of 13)**. A
14-day-excluding-today-in-UTC Davi says **64.3% (9 of 14)**. Two products, one
database, two numbers on the same phone in the same minute.

**Delivery: tool only** (`get_health_record`, topic `medication_adherence`), plus a
legacy handler. A 30-day rolling window changes daily — the worst possible profile
for anything prompt-resident.

**This needs a parity test** asserting Davi's counts equal Spring's for a fixed
fixture, or the two drift on Spring's next change. §8 Q1 covers the alternative
(call Spring's endpoint) and why it is not the default.

### 2.4 `mood_log` (Stage 2)

Mapping: `id` (BigInteger variant), `user_id`, `log_date` (Date), `score`
(SmallInteger), `updated_at` — **not `created_at`**: an evening correction
replaces the score, and the time shown must belong to the score shown
(`db/existing_schema.sql:2452-2455`). `factors text[]` deliberately unmapped — the
codes come from a Java `MoodFactor` enum Davi does not have; rendering raw codes is
garbage and inventing labels invents meaning.

```python
async def mood_scores(db, user_id, *, days=30, limit=60) -> list[tuple[date, int]]:
```

`user_id = ? AND log_date >= today - days`, `ORDER BY log_date DESC, id DESC`
(`MoodLogRepository.java:34`). Owner-only: no privacy flag, not in
`resource_type_enum`.

Three rules:

- **No zero-fill.** An unlogged day is an absence, not a neutral score
  (`MoodLogRepository.java:28-33`).
- **No band labels.** The seven display bands are derived by Spring's `MoodScale`
  on read and deliberately not stored (`entities/MoodLog.java:56-64`). A second,
  disagreeing vocabulary in front of the same reader is worse than a bare number.
- **Report, never interpret.** "Your mood averaged 4.1 over two weeks, down from
  6.8" is a report. "That suggests depression" is diagnosis, blocked by
  `app/chat/validation.py`. The triage floor runs first on a mood turn as it does
  on everything else — no handler routes around it.

**Delivery: tool only.** A mental-health score in every prompt forever, including
"what is paracetamol", is a privacy cost with no matching consultation frequency.

### 2.5 Allergies on the drug path — the one wiring change that is not a read

**The `[P]` block does not reach the drug path.** VERIFIED by tracing
`app/chat/orchestrator.py`:

- Step 5, the drug-information handler, is `orchestrator.py:577-609` and `return`s
  a `ChatResult` at `:598`.
- `build_patient_context` is called at **`orchestrator.py:615`** — after it.
- `_memory.append_to(patient_text)` is at `:686`.

So a reader with a severe penicillin allergy who asks **"side effects of
amoxicillin"** (VERIFIED: `extract_drug_query_term` returns `'amoxicillin'`) gets a
monograph with no allergy mention. It *cannot* mention it — the signature makes it
impossible:

```python
# app/drugs/service.py:218  (VERIFIED)
def build_drug_reply(drug: DrugReference) -> str:
```

Worse, step 5 sits after the engine branch at `orchestrator.py:461`, so it is
**legacy-only** — and legacy is the default. On `CHAT_ENGINE=agentic` the same
question reaches the model with `[P]` assembled at `:1074`, allergy present. That
is precisely the engine split CLAUDE.md already records for the drug-interaction
refusal, pointing the other way.

**The fix is one parameter, not a handler:**

```python
def build_drug_reply(drug: DrugReference, allergy_warning: str = "") -> str:
    ...
    if allergy_warning:
        parts.insert(0, allergy_warning)
```

with `orchestrator.py:593` building the string from
`health_records(kinds=("allergy",))` filtered to
`category == "medication" and severity == "severe"`. Deterministic,
validator-safe, one call site. Both engines covered: legacy by the parameter,
agentic by `[P]`.

Note step 3.4, the drug-combination refusal (`orchestrator.py:445-455`), also
returns before `[P]`. That one is a refusal, so failing closed is fine — but stop
claiming `[P]` is universal.

### 2.6 Two columns on models that already exist

```python
# app/models/coredata.py, MedicineTracking (after :241)
deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

# app/models/core.py, User (after :60)
timezone: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)  # db/existing_schema.sql:2037
```

`User.gender` is already mapped (`app/models/core.py:60`, `String(16)`) — that is
the sex source for the Stage 0 bracket fix.

### 2.7 The tool: +1, not +6

One new `ToolSpec` in `app/chat/tools/definitions.py`, appended to `TOOL_SPECS`
(`:234-245`), one line in `EXECUTORS` (`app/chat/tools/registry.py:29-40`):

```python
GET_HEALTH_RECORD = ToolSpec(
    name="get_health_record",
    description=(
        "Look up what the reader has stored in their own health record: "
        "diagnosed conditions, past surgeries, allergies, how consistently they "
        "have taken their prescribed medication, or their mood log. Use this "
        "whenever an answer turns on what is actually on their record rather "
        "than on general knowledge. An empty record means nothing has been "
        "entered — say that, and never that they are unaffected."
    ),
    input_schema=_obj(
        {"topic": {"type": "string",
                   "enum": ["conditions", "surgeries", "allergies",
                            "medication_adherence", "mood"]},
         "days": {"type": "integer",
                  "description": "Lookback for medication_adherence and mood. Defaults to 30."}},
        ["topic"],
    ),
)
```

Why one tool and not five: the schema bytes are not the constraint — a tool schema
sits inside the cached prefix at ~17 effective tokens/turn on a cache hit — but
**one extra agent round costs ~2,736 effective tokens** (the ~2,313-token volatile
suffix is re-sent uncached each round). Break-even: if a bigger tool menu causes a
wrong-first-tool round on more than **~6.2%** of turns, the schema saving is
irrelevant. Five sibling tools with descriptions overlapping the existing
`get_condition_guidance` and `get_latest_metric` is exactly how that rate goes up.

MEASURED (`python -m scripts.cache_probe --measure`, plus rendering the spec above
through `app/rag/prompt.py:32`):

| | est. tokens |
|---|---|
| Today's cacheable prefix | 2,541 (system rules 850 + 10 tool schemas 1,691) |
| `GET_HEALTH_RECORD` spec | **229** |
| New prefix | **2,770** |

2,770 clears Sonnet 5's 1,024 minimum and Opus 5's 512, and stays definitively
below Haiku 4.5's 4,096 (`app/llm/anthropic.py:133-148`) — so Haiku's breakpoint
stays the no-op it already is, rather than straddling the ±25% band where the probe
refuses to give a verdict.

Executor is thin, no try/except — the registry owns that
(`app/chat/tools/executors.py:15-17`):

```python
async def get_health_record(db, user_id, args, _session_id) -> dict | None:
    topic = str(args.get("topic", "")).strip()
    ability = await handle_health_record_query(db, user_id, topic, int(args.get("days") or 30))
    return _unwrap(ability, topic=topic)
```

`get_health_record` is a pure DB read, so it stays **out** of
`UNTRUSTED_VALUE_TOOLS` (`registry.py:45`) and its content becomes a legitimate
fidelity source.

**But the fidelity guard does not protect adherence counts.** VERIFIED,
`app/grounding/fidelity.py:33-38`: `_UNIT_VALUE_RE` extracts a BP pair, a
percentage, or a number followed by
`mg/dl|mmhg|mmol/l|mcg|mg|g/dl|g|ml|iu|bpm|kg`. `"12 of 14 doses"` contains **no
unit**, so neither number is extracted and neither is checked — the model can state
"you missed 9 of 14" and nothing catches it. Percentages *are* covered. So: render
adherence as `"85.7% (12 of 14 doses)"` in `deterministic_reply` — the percentage
gives the guard something to check, and the prompt already tells the model to
prefer `deterministic_reply` verbatim.

`tests/test_chat_tools.py:52` asserts
`{s.name for s in TOOL_SPECS} == set(EXECUTORS)`, so a half-wired tool fails the
suite. That is the wiring test; no new one needed.

**Legacy engine.** `CHAT_ENGINE` defaults to legacy, which offers **no tools at
all**, so a tool-only read reaches nobody today. Each topic needs a deterministic
handler in `app/chat/data_handlers.py` returning the standard
`{reply, action, provenance}` (`handle_summary_query` at `:698` is the template),
wired into the legacy chain at step 4. Conditions and allergies do not need one —
they arrive via `[P]` and via `build_drug_reply`.

---

## 3. What Davi must NOT read

Seven tables. Names never added to `COREDATA_TABLES`, no model, no read function.

| Table | Line | Why never |
|---|---|---|
| `refresh_token` | `db/existing_schema.sql:269-284` | `token_hash varchar(64)` is a **live session credential**; `ip_addr inet` + `user_agent` are a device and location fingerprint. Reading it puts authentication material into model context and, via compaction or a receipt, into the storage the "no PHI in logs" invariant exists to keep clean — receipts store a SHA-256 of the message *precisely* so raw content never lands there. There is no health question behind it: "how many devices am I signed in on" is an account-settings screen in mhn-react. Highest-churn table in the list (one row per login and per rotation). |
| `device_token` | `:2054-2077` | `token text NOT NULL UNIQUE` is a **push credential** — anyone holding it can send notifications to that device. Same line as above. |
| `subscriptions` | `:1041-1062` | Billing: `payment_id`, `payment_status`, `payment_method`. A chat assistant discussing payment state opens a refunds/entitlement/compliance surface for zero clinical value. If "am I premium" is ever needed for feature gating it belongs in the JWT claim `app/auth.py` already validates, not a table read. |
| `event_log` | `:1072-1085` | The **audit table**: unbounded `payload jsonb` with no schema, written by every service in the estate. Reading it into a prompt is a prompt-injection surface with third-party writers, of unknown and changing shape. |
| `notification` | `:2084-2117` | Pre-rendered push `title`/`body` strings duplicating their source tables. Reading it means paraphrasing a notification instead of answering from the record. Several hundred to a few thousand rows/user/yr. |
| `sos_contact` | `:292-306` | A named **third party's** phone number and email, who never consented to being described to an LLM. The emergency path is already deterministic canned text (`app/chat/replies.py`) with the SOS button owned by the app. |
| `report_parameter_value` | `:2741` | Not a privacy refusal — a **data** refusal. Zero writers in both sibling repos (VERIFIED). A rewrite of `recent_lab_values` onto it silently loses every lab value. Add it the day a producer exists; note then that it has no privacy column of its own and its gate is the parent document addressed by `(section, record_id)`. |

**Skipped as dead or redundant:** `sleep_sessions` (no writer — shipping a sleep
answer that is always "no data" is a regression dressed as a feature);
`lifestyle_daily_total` / `weekly_total` (`app/coredata/service.py:545` already
computes short windows correctly from `lifestyle_log`; a second source that can
disagree is worse than one); `lifestyle_monthly_total` (only wins on "how much did
I drink last year", and drags in the calendar-vs-rolling window mismatch);
`period_day_log` (symptom detail behind two traps, for a question `period_tracking`
mostly answers).

**Columns never selected, even from mapped tables:**
`medical_condition.family_linked_relations` (`:2853`) — another person's health
status inside the reader's own row; `medical_condition.notes` — unbounded clinical
free text, tool result only; `mood_log.factors` and `period_day_log.symptoms` —
Java enum codes Davi does not have; `flow_intensity_enum` in any SQL comparison or
`MAX()` — the V1 declaration order sorts `spotting` above `heavy` and the type
cannot be reordered (`db/existing_schema.sql:1710`).

---

## 4. The memory document — final field list

Rendered inside the existing `[P]` block via
`memory_assembly.UserMemory.append_to` (`app/chat/memory_assembly.py:66`).

**No `[M]` marker.** VERIFIED: `MARKER_RE = re.compile(r"\[(\d+|P|GK)\]")`
(`app/grounding/claims.py:18`). An unregistered marker raises `invalid_marker`
(`claims.py:170-176`) on **every** memory-cited sentence — under
`GROUNDING=enforce` that fails or rewrites answers wholesale; under `log` it floods
the warning channel.

| # | Field | Source | Cap | Typical | At cap | What it buys |
|---|---|---|---|---|---|---|
| 1 | Identity band (age / sex / pregnant) | `user_profiles`, `app/chat/profile.py:245` | — | 23 | 23 | Age gates screening advice; pregnancy is a hard contraindication gate. Changes ~never, consulted almost every clinical turn. |
| 2 | Diagnosed conditions | `medical_condition` type=condition | 5 | 42 | 60 | The largest gap in the surface: Davi knows the reader's *family* history and not their own diagnoses. `condition_code` also drives RAG scope with no extra query. |
| 3 | Allergies, severe first | `medical_condition` type=allergy | 4 | 25 | 28 | **The only field whose absence is itself a safety risk.** Cheapest safety token in the document. |
| 4 | Current medicines | `medicine_tracking`, `service.py:569` | 6 | 36 | 60 | Consulted on every interaction / side-effect / "can I take X" turn. |
| 5 | Latest labs | `content.ai` via `recent_lab_values` `:718` | 5 | 40 | 50 | Capped at 5, down from the live path's 14 — unbounded values are fine gated on a personal query, ruinous always-on. |
| 6 | Open symptom episodes | `app/chat/episodes.py:184` | 3 | 22 | 33 | The whole point of conversational continuity ("still not better"). |
| 7 | Family history + insight tiers | pedigree, `app/chat/context.py:57` | — | 42 | 42 | Today's `[P]` block, unchanged. De-identified aggregate only — never a named relative, never anything sourced from `can_view_document`. |
| 8 | Tracker targets | `lifestyle_limit` | 5 | 12 | 14 | Makes any tracker total mean something without Davi inventing a guideline. |
| 9 | Document count + newest date | own rows only | — | 10 | 10 | Stops the model claiming the shelf is empty. 10 tokens instead of 49 for a list `get_documents` already serves. Count **only** the reader's own rows, never the family-visible set. |
| | **Total** | | | **~252** | **320** | |

Measured with the repo's own estimator (`app/rag/prompt.py:32`, chars/3.5 — the
same convention `model-cost.md` and `scripts/cache_probe.py:70` use). A brand-new
user renders ~27 tokens. The 900 ceiling is only reachable by including fields that
fail the value test.

At `project_docs/model-cost.md:96`'s basis (~$109 per token per month at 1M users),
252 instead of 900 saves ~$70K/month at 1M.

**Cut, and why:**

| Cut | Tokens saved | Reason |
|---|---|---|
| **Trends** | ~31 | Needed `report_parameter_value`'s `(user_id, thp_id, measured_on)` index to be one `GROUP BY`. Over `content.ai` envelopes it is a full re-parse of every document, every turn. Revisit the day a writer exists. |
| Vitals + body/BMI | 57 | Already read live by `build_health_snapshot` (`app/chat/context.py:189`) gated on `is_personal_health_query` (`orchestrator.py:621`). `vital_reading` is the fastest-moving field in the set. |
| Lifestyle totals, sleep, activity, mood | 104 | Aggregates over fast-moving logs, consulted only when the reader raises the topic. Mood additionally carries a privacy cost. |
| Dose adherence | 28 | Closest call; loses on frequency, not value. A 30-day rolling window changes daily. First topic on the tool; promote it if telemetry shows adherence questions are common. |
| Document list → count | 39 | `get_documents` already serves the list on demand. |
| **App-inventory line** | 105 | Moves to the **cached system rules**, not the block. It is byte-identical for every user, and a per-user block is uncacheable by construction: the same 105 tokens costs ~$11.5K/month at 1M in the doc versus ~$1.1K in the prefix. **General rule: nothing constant across users may live in the per-user block.** |
| All `period_*` | 30 | See §5. |

**Field 1's pregnancy flag, stated precisely so the next agent does not draw the
wrong rule:** `is_pregnant` is in the block because it is in `WRITABLE_FIELDS`
(`app/chat/profile.py:39-48`), gated on `chat_personalization`, and volunteered by
the reader — **not** because `user_profiles` is a safer table than `period_status`.
The privacy property belongs to the fact, not to the column it came from. A support
screenshot of a paracetamol conversation showing "pregnant: yes" is equally bad
either way.

**Gates, both reusing what exists:** the block read **and the background rebuild**
go through the erasure short-circuit at `app/chat/memory_assembly.py:83`/`:160` — a
rebuild that skips it reconstructs a deleted memory from undeleted sources, and
erasure quietly becomes cache invalidation. Consent reuses `chat_personalization`
(`app/chat/profile.py:32`); no second grant, and no grant means falling back to
today's live assembly.

**The second cache breakpoint is NOT in this plan.** See §8 Q3.

---

## 5. Consent and privacy, table by table

| Table | Own flag? | Filter Davi applies | Spring rule honoured |
|---|---|---|---|
| `medical_condition` | `private` (default false) + `deleted_at` | Owner: `deleted_at IS NULL` only. Family: `+ private IS FALSE` (NULL excluded) + `can_view_document(..., "medical_condition", id, ...)` | `MedicalHistoryRepository.java:23` (owner, no filter); `FamilyServiceImpl.java:636` + `:651-661` (family) |
| `medicine_dose_log` | none | Owner-only. Join `medicine_tracking`, `deleted_at IS NULL`. No viewer parameter, ever. | `MedicineDoseLogRepository.java:37` filters `user_id` only — safe because every dose endpoint is owner-only |
| `mood_log` | none | Owner-only: `user_id` + date range | `MoodLogRepository.java:26`, `:34` |
| `lifestyle_limit` | none | Owner-only: `user_id` + `effective_from <= day` | `LifestyleLimitRepository.java:52` |
| `medicine_tracking` (existing) | `private` + `deleted_at` | **Add `deleted_at IS NULL`.** `private` — see §8 Q2 | Spring never filters `private` on this table (zero predicate hits) |
| `period_settings` / `period_status` / `period_tracking` | `private` **defaults TRUE** | **Owner-only, no exceptions. `can_view_document` MUST NOT be used.** Gate on `enabled`; silent while `paused_until >= today` | `PeriodSettings.java:59-72` — the inversion is deliberate, and `resource_type_enum` was deliberately not extended with a cycle value (`ResourceType.java:5`) |

**The one gate that must not be reused.** `resource_type_enum` has no cycle value,
so `family_file_access` and `file_access_exclusions` structurally cannot express
cycle sharing — `can_view_document` would return **True** for a family viewer.
Copy-pasting the document gate onto the period tables is the single highest-severity
mistake available in this whole plan: it hands every accepted family connection
visibility of contraception and pregnancy status nobody opted into, which is the
exact outcome the sibling team inverted the default to prevent.

**The trap in the inherited cases:** a plain `WHERE user_id = :owner` returns rows
the owner has marked private, with no error and no indication anything was skipped.

**The exclusions gate fails OPEN.** VERIFIED, `app/coredata/service.py:325-333`:

```python
    except Exception:  # noqa: BLE001 — table may not exist on standalone DBs
        return {}
```

An empty exclusion map means *nothing is excluded* — the failure grants more
access. Defensible today (connection-level grant checked first; payload is document
titles). Routing `medical_condition` through it changes the payload to **a diagnosis
list**. Failure scenario: transient pool exhaustion during the exclusion query,
`except Exception` swallows it, and a family viewer sees an HIV or psychiatric
diagnosis the owner had explicitly excluded for exactly them. **Fix in Stage 0 #4:**
narrow the `except` to the missing-relation case
(`ProgrammingError`/`OperationalError`) and let anything else propagate to the
caller's fail-open, which returns *no* records rather than *all* of them.

**Do not add speculative `viewer_id` parameters.** Stage 1 delivers conditions via
the owner-only `[P]` block; there is no family caller. Untested consent code on a
path nothing exercises is worse than no code — the next agent copies it as an
established pattern and it has never once been run. Add it the day a family
medication or condition read exists.

---

## 6. Sibling compatibility

No Flyway migration, no mhn-spring change, no mhn-ai change. Every table already
exists. The compatibility work is entirely about reading the siblings' semantics
correctly.

### Spring predicates, per table

| Table | Spring's exact predicate | File |
|---|---|---|
| `medical_condition` (owner) | `user_id = ? AND type = ? ORDER BY started_on DESC`, no privacy filter | `MedicalHistoryRepository.java:23` |
| `medical_condition` (family) | `user_id = ? AND private = false`, minus `file_access_exclusions`, behind accepted-connection + `reqRead`/`accRead` | `FamilyServiceImpl.java:577-590`, `:636`, `:651-661` |
| `medicine_dose_log` (day) | `user_id = ? AND scheduled_date = ? ORDER BY (scheduled_time IS NULL), scheduled_time, tracking.name` | `MedicineDoseLogRepository.java:37-45` |
| `medicine_dose_log` (adherence) | 30-day default, inclusive of today, reader's zone, PRN and pending excluded | `MedicineTrackingServiceImpl.java:383-419` |
| `mood_log` | `user_id = ? AND log_date BETWEEN ? AND ? ORDER BY log_date DESC` | `MoodLogRepository.java:26`, `:34` |
| `lifestyle_limit` | latest row with `effective_from <= day` | `LifestyleLimitRepository.java:52`, `LimitSchedule.java:57-70` |
| `lifestyle_*_total` | `user_id + bucket_start` range on the grain table; a bucket that empties is **DELETED**; weeks open **Sunday**; the day is resolved in `app.tracking.zone` at write time | `LifestyleRollupDao.java:135-186`, `TrackingGrain.java:47-52` |
| `period_status` | `findFirstByUserIdAndEffectiveFromLessThanEqualOrderByEffectiveFromDesc` | `PeriodStatusRepository.java:29` |
| `period_tracking` (stats) | `counts_toward_stats = true AND end_date IS NOT NULL AND start_date >= ?` | `PeriodTrackingRepository.java:64-71` |

**Spring already serves adherence live** — VERIFIED, and the design missed it:
`GET /courses/{trackingId}/adherence` (`MedicineController.java:149-153`) →
`AdherenceResponse(trackingId, from, to, total, taken, skipped, forgotten, pending,
adherencePct)` (`MedicineDtos.java:276-287`). See §8 Q1.

### mhn-ai vocabulary Davi must read correctly

| Concept | Values | Rule |
|---|---|---|
| `abnormal_flag` | `low` / `normal` / `high` / null | **Authoritative** — computed in deterministic Python, never by the model. Never recompute it. |
| `flagged_against` vs `reference_range` | — | **Quote `flagged_against`.** `app/models/ai_results.py:129-135` is explicit: with an ideal-range override the two are different numbers, and the printed one is a limit the value never crossed. Davi renders the wrong one today — Stage 0 #1. |
| `range_source` | `ideal_range` / `report_range` / `none` | `report_range` + NULL flag means a range was present and could not be decided — genuinely different from `none` (no range printed at all). Do not collapse them. |
| Run item status | `pending, queued, processing, classifying, extracting, generating_insights, completed, failed, rejected, cancelled` | `TERMINAL_STATUSES` is exactly `{completed, failed, rejected, cancelled}` (`app/models/enums.py:31-39`). Everything else is in flight — never "stuck". |
| Name check | `match` / `mismatch` / **`unknown`** | `unknown` is a first-class third answer: no name was printed (a bare X-ray, most vaccination cards). Never narrate it as a mismatch; never suggest a retry on `mismatch` (`last_error_code="name_mismatch"`). |
| Match key | lowercase, **all whitespace removed**, exact dict lookup, names registered before aliases | `app/services/ideal_ranges.py:69`, `:107`. Never a substring, never a `LIKE`. This is the shape the substring bug should have had. |

**Two bugs in mhn-ai that Davi must NOT copy:**

- `pick_bracket` (`app/services/ideal_ranges.py:151-171`) has no `sex` predicate —
  the same hole as Davi's `reference.py:168-176`. Fix Davi properly; raise it with
  that team.
- Both services read `traditional_health_parameters.aliases`, frozen pre-V18 by
  design (`V14:355`). V18's 1,184 curated aliases live in `thp_alias` only, so both
  match a stale set. §8 Q5.

---

## 7. Build order

Each stage ships and is useful alone. Per stage: register each `__tablename__` in
`COREDATA_TABLES`, re-run the `pg`-marked coexistence check. No
`db/flyway/V*__davi_*.sql` is involved, so
`tests/test_flyway_parity.py::FLYWAY_TABLES` needs no entry.

### Stage 0 — four bug fixes, independent of everything else, shippable today

| # | Fix | File:line | Failure it removes |
|---|---|---|---|
| 1 | Render `flagged_against`, not `reference_range` | `app/api/v1/documents.py:58` (the flag comes from `:53`) | Screen shows a "high" flag beside a limit the value never crossed. One-token fix, patient-facing. |
| 2 | Add `MedicineTracking.deleted_at` + `.is_(None)` to `active_medications` | `app/models/coredata.py:241`, `app/coredata/service.py:569-595` | A medication deleted in the MHN app has `stopped_at` NULL and is still reported as currently taken — the wrong input to any medication question. |
| 3 | Sex-aware age bracket: `sex IN (<reader's gender>, 'any')`, prefer the specific row | `app/health/reference.py:168-176`; map `ThpAgeRange.sex` | V14 added `thp_age_range.sex`; V18 loaded 78 sex-specific rows (199 `any`, 38 `male`, 40 `female`). A woman graded against the male haemoglobin band is told a normal value is low. **Source is `User.gender`** (`app/models/core.py:60`; PG `gender_enum` = male/female/other, vocabulary matches `thp_age_range.sex` exactly) — **not** `UserProfile.sex` (`app/models/profile.py:52`), an unvalidated self-reported `String(16)`: a reader who typed "F" matches no row and fails silently, which is the exact bug class being fixed. `gender='other'` and `gender IS NULL` both fall back to `sex='any'`, never to `ranges[0]`. |
| 4 | Narrow `_viewer_exclusions`' `except Exception` to the missing-relation case | `app/coredata/service.py:325-333` | Prerequisite for Stage 1 — see §5. |

**Raise, do not fix alone:** `add_lifestyle_log` (`app/coredata/service.py:524-543`)
inserts a `lifestyle_log` row and never folds the delta into Spring's three rollups,
which `LifestyleRollupDao.apply` (`:45-72`) maintains at write time.
`ManualTrackingReconciler.java:38` repairs only a **3-day trailing window** at 03:15,
so a backdated chat entry is wrong forever and the reader's own chart in the MHN app
is understated. Do **not** replicate the three-table delta in Davi — it means
duplicating their Asia/Kolkata day resolution and Sunday week boundary. §8 Q6.

### Stage 1 — `medical_condition` + `lifestyle_limit`

Two models, `health_records()` + `lifestyle_limits()`, memory fields 2/3/8,
`lifestyle_limits` folded into `build_health_snapshot`'s existing lifestyle line, and
the `build_drug_reply(drug, allergy_warning)` parameter (§2.5). **No tool.**

Ships alone: a severe penicillin allergy in front of the model *and* in the
deterministic drug reply, before it answers a medication question. Highest-value
change in the plan.

### Stage 2 — the tool

`get_health_record` with topics `conditions | surgeries | allergies |
medication_adherence | mood`; the `medicine_dose_log` and `mood_log` models;
`dose_adherence()` + `mood_scores()`; `User.timezone`; the legacy handlers; one line
each in `TOOL_SPECS` and `EXECUTORS`; the Spring parity test for adherence.

Ships alone on `CHAT_ENGINE=agentic`; the legacy handlers cover the default engine.

### Stage 3 — memory document assembly

Fields 1–9 rendered inside `[P]`, capped and deterministically ordered,
consent-gated on `chat_personalization`, erasure-gated on
`memory_assembly.py:83`/`:160`, app-inventory line moved into the cached system
rules. No new tables.

### Stage 4 — cycle (conditional, §8 Q4)

`period_settings` / `period_status` / `period_tracking`, `cycle_context()`,
owner-only with no viewer parameter anywhere in the call chain, gated on `enabled`,
silent while `paused_until` is in the future. **Not** a value on the tool enum unless
the executor is gated on an explicit cycle keyword in the current message.

---

## 8. Open questions

**Q1 — Adherence: call Spring's endpoint, or mirror its semantics?**
Spring serves `GET /courses/{trackingId}/adherence` (`MedicineController.java:149`).
Calling it inherits the window, the reader's timezone, the PRN rule and the pending
rule for free, and cannot drift. But it is per-course (N calls for N medications) and
Davi has no verified service-token path to Spring — the existing `MHN_SERVICE_TOKEN`
pattern (`app/documents/service.py`) points at mhn-ai, not Spring.
**Recommendation: mirror the semantics (§2.3) with a parity test asserting Davi's
counts equal Spring's for a fixed fixture.** One query, no new integration, and the
test catches drift. Revisit if Spring changes the computation more than once.
*Needs from the Spring team: confirmation that the five rules in §2.3 are stable.*

**Q2 — What does the `private` tickbox on a medication actually say?**
`app/coredata/service.py:583` filters `MedicineTracking.private.is_(False)` on the
reader's **own** snapshot, so a reader who marks a medicine private has it hidden
from their own assistant. The obvious fix is to drop the filter — but the premise
that `private` is "the family-sharing flag" is **wrong for this table**:
`medicine_tracking` is not in `resource_type_enum` (`ResourceType.java:5`), and
grepping Spring's family module for `medicine` returns zero hits. Spring sets and
returns the flag (`MedicineTrackingServiceImpl.java:131`, `:216`, `:620`) and
**never filters on it**. Its meaning is undefined by behaviour.
**Recommendation: ask what the UI label says before flipping it.** If it reads "hide
from my assistant", removing the filter does the opposite of what the reader asked —
into an always-on prompt block. Note
`tests/test_personalization.py:129 test_active_medications_excludes_stopped_and_private`
asserts the current behaviour and would need deleting.

**Q3 — Is a second cache breakpoint on the memory block worth building?**
Not until it is measured. The claimed ~71% saving assumes a 6-turn session with no
evidence; for a **single-turn** session — a health chat's most common shape — a
breakpoint pays a 1.25× write premium and never reads, making that turn 25% *more*
expensive. Two of the nine fields also mutate mid-session (open episodes; document
count, since chat has `/chat/upload`), each change paying another 1.25×. And the
refactor is not the one-line `anthropic.py:205` change it was described as:
`patient_text` is currently folded into the **volatile** block
(`orchestrator.py:783`), so it must be split out of `build_system_prompt` and
reordered in **both** engines (`:783` legacy, `:1111`/`:1125` agentic, where `:1125`
also concatenates a per-turn directive).
**Recommendation: ship Stage 3 without it. Measure the turn-count distribution and
run `python -m scripts.cache_probe --model <production model id>` first.** The probe
deliberately refuses to print a hit rate it did not observe, because a breakpoint
that fails to cache is invisible from inside the application — the reply is
byte-identical and only `usage.cache_read_input_tokens` differs. Do not route around
it.

**Q4 — Cycle data: in scope or not?**
Nothing in the user's request names it, and it is the highest-sensitivity surface in
the schema. The stated rule "never auto-invoked from inference" is **not enforceable
as a tool enum value** — an enum value is precisely a thing the model chooses, and
`registry.execute_tool` (`app/chat/tools/registry.py:64-116`) has no mechanism to
reject a call because the reader did not use the right words. Failure scenario: the
reader asks "why have I been so tired lately?", a fatigue differential includes
anaemia from heavy menstrual bleeding, the model helpfully calls `topic="cycle"`, and
the answer surfaces contraception status the reader never raised.
**Recommendation: cut Stage 4 from the build.** If it is wanted, ship it as a legacy
handler gated on an explicit cycle keyword in the current message, never as an enum
value. Surfacing pregnancy status as context is in scope; inventing
pregnancy-specific dosing guidance is new clinical content and takes the DRAFT +
review path, not a handler change.

**Q5 — The stale alias set.** Both Davi (`app/health/reference.py:149`) and mhn-ai
(`app/services/ideal_ranges.py:123`) read
`traditional_health_parameters.aliases`, frozen pre-V18 by design (`V14:355`). V18's
1,184 curated aliases live in `thp_alias` only, so both services silently degrade to
"unmatched" on any lab test named by a curated alias.
**Recommendation: out of scope here, but raise it with the mhn-ai team so both
services switch to `thp_alias` (unique per alias; filter out `status='rejected'`) in
the same change.** Fixing one alone puts two services on different vocabularies.

**Q6 — Who folds Davi's tracker writes into Spring's rollups?**
Options, cheapest first: (a) accept the staleness and document the window; (b) have
Davi call Spring's tracking endpoint instead of `INSERT`ing, inheriting the zone, the
Sunday week boundary and the limit check for free; (c) replicate the three-table
delta in Davi.
**Recommendation: (b) if such an endpoint exists, else (a). Never (c).**
Related and minor: Davi stamps provenance as
`metadata_json={"source": "davi_chat"}` (`service.py:537`) while V14 added a
first-class `lifestyle_log.source varchar(16)` (`:2874`) with vocabulary
`quick_add|search|manual|reminder_action` — Davi's rows take the `manual` default, so
Spring cannot tell a chat entry from a typed one.

---

## 9. Where the design and the review disagreed

| Point | Winner | Why |
|---|---|---|
| Allergies via the `[P]` block alone | **Review** | Traced the path: five `return`s sit before `build_patient_context` at `orchestrator.py:615`, and the drug handler at `:577-609` is one of them — and is legacy-only. A `build_drug_reply` parameter is both smaller and correct. |
| `dose_adherence` defaults (14 days, excludes today, UTC) | **Review** | Spring computes adherence live at `MedicineTrackingServiceImpl.java:383-419`: 30 days, inclusive, reader's zone. Three of five semantics disagreed. Two numbers on one phone. |
| Second cache breakpoint as a headline win | **Review** | Arithmetic understated by 24%, single-turn case unmodelled and sign-inverting, refactor under-scoped. Measure first. |
| One tool, not six | **Design** | Correct, for the right reason: the break-even is the wrong-tool round (~6.2%), not schema bytes. Token estimate corrected 170 → **229**; conclusion unchanged. |
| Refusing `report_parameter_value` and `sleep_sessions` | **Design** | Independently re-verified. The most valuable single call in the whole exercise. |
| `medical_condition` reusing `can_view_document` | **Design** | `ResourceType.java:5` includes it and `FamilyServiceImpl.java:635-639` uses exclusions on it. Reuse it; do not write a second gate. |
| Dropping `private` from the owner's own medication snapshot | **Review** | The generalisation is wrong for this table; ask what the tickbox says. §8 Q2. |
| Cycle as a tool enum value | **Review** | An enum value is model-chosen; "explicit ask" as prose is not a gate. §8 Q4. |
| Speculative `viewer_id` parameters | **Review** | Untested consent code on a path nothing exercises gets copied as an established pattern. |

**Everything in §2 is read-only over tables that already exist. Zero schema risk is
why this is cheap, and it is why the whole ask is achievable.**

# CLAUDE.md — architecture map & current state

Orientation for future agents. Read this plus the README before changing code.

## What this is

A healthtech backend with two independent halves that share a database and auth:

- **Insights engine** (`app/insights/`): pure, deterministic family-history risk
  → template insights. **No LLM.** This is the reproducible, auditable heart.
- **Chatbot chassis** (`app/chat/`, `app/rag/`, `app/grounding/`, `app/llm/`,
  `app/triage/`): deterministic triage floor → RAG → LLM → grounding →
  validation → receipts, with structured compaction.

Everything is decision support, never diagnosis — enforced in code.

## Development Workflows

Reusable workflow instructions are stored in `.claude/`.

### Implementation
When executing an existing implementation plan, read:
`.claude/execution-rules.md`

Follow those instructions for implementation, verification, testing, and final diff review.

### Code Review
When performing an independent review of implemented changes, read:
`.claude/review-rules.md`

Follow those instructions for requirement coverage, bug detection, security, performance, testing, and review findings.

### Task Plans
Current implementation plans and task-specific documents are stored in:
`project_docs/` (architecture.md, drawbacks.md, implementation-plan.md).
`docs/` holds the verified production contracts, not plans.

Treat the current task's implementation plan as the source of truth for what should be implemented.

Do not assume these workflow files apply automatically to every task. Read the relevant workflow file when the task matches its purpose.

## Database coexistence (important)

This backend shares the **MHN production database**. The production stack is
three repos (see `docs/production_integration.md` for verified contracts):
`mhn-spring` (Java API, **Flyway owns ALL schema** — including the `ai_*`
tables since V4; our `V6__davi_ai_tables.sql` is ADOPTED, and teammates have
added V7–V10 after it — V10 is the ai name-check columns), `mhn-ai`
(per-document AI: classify → file → extract → insights; writes `content.ai`
envelopes into `reports.content` etc.; its Alembic chain is frozen,
`ai_alembic_version`; since its PR #4 it also runs a NAME CHECK — a document
whose printed patient name doesn't match the wallet owner is NEVER filed:
item status `failed` + `last_error_code="name_mismatch"`, the status
endpoint carries `name_check {verdict, document_name, confirmed}` +
`filed_section`/`section_row_id`, and the typed ai-result route answers 409
for mismatches — `fetch_ai_result` reads all of this and the chat explains
the mismatch instead of suggesting a retry), and `mhn-react` (BFF frontend;
our chat PRs are merged on main, plus the team's name-mismatch dialog).

Rules we follow (do not break these):

- Our tables use **plain `uuid` `user_id` columns with NO FK** to `"user"`
  (matching `ai_processing_runs`). FKs among our OWN tables are fine
  (`pedigree_conditions.consent_grant_id`, `insight_artifacts.superseded_by`,
  the `conversation_*` cascade).
- **Production schema ships as Flyway**: `db/flyway/V6__davi_ai_tables.sql` is
  the DDL for adoption into mhn-spring's migration chain. Our Alembic chain
  (version table **`davi_alembic_version`**) builds local/test databases only.
  The `"user"` table is in ORM metadata (partial read-only `User` model) but
  **excluded from migrations** via `EXTERNAL_TABLES` + `include_object`.
- Migrations are verified two ways: reversibility on a fresh DB, and coexistence
  (apply on top of a full load of `db/existing_schema.sql` — note that dump is
  the V1 baseline; production has ddl-auto additions like
  `family_connect.req_read/acc_read` and `file_access_exclusions`, which our
  models map nullable-with-fallback).
- **Auth**: production session JWTs are **HS512** with the shared `JWT_SECRET`
  **Base64-decoded** before HMAC (mirrored via `JWT_SECRET_BASE64`); `sub` =
  user UUID. Optional `SERVICE_TOKEN` + `X-User-Id` server-to-server path
  mirrors the Spring↔mhn-ai `AI_TOKEN` pattern.
- **mhn-ai's output is read, never written**: lab values come from
  `content.ai.extraction.results[]` (`test_name`, `value_numeric`,
  authoritative `abnormal_flag`); listing titles from
  `content.ai.classification.title`. Family reads honour
  `req_read`/`acc_read` (owner-side) + `file_access_exclusions`.

## Layout

```
app/
  config.py            pydantic-settings (env)
  db.py                async engine + Base + get_db
  auth.py              HS256 JWT, DEV_USER_ID, authorize_user (object-level authz)
  main.py              app factory + router wiring
  models/              ORM: common (mixins, JSONColumn, EmbeddingType), core,
                       rules, chat, jobs  (+ EXTERNAL_TABLES)
  insights/
    constants.py       DRAFT clinical constants (onset midpoints, weights, copy)
    core.py            PURE engine — assemble_facts, 6 patterns, evaluate,
                       aggregate, render_insight, content_hash. 100% branch cov.
    engine.py          recompute_insights (DB) — hash-supersede, retraction
  triage/red_flags.py  DRAFT phrase tables; triage() floor (apostrophe-insensitive)
  chat/
    scope.py           off-topic decline
    router.py          deterministic intent routing (triage floor wins)
    validation.py      banned-phrase + HIGH/EMERGENCY reassurance block
    replies.py         canned + safe replies (all pass the validator)
    memory.py          PURE compaction extractors + merge (sticky/capped)
    conversation.py    session/message persistence + maybe_compact + assemble_context
    context.py         patient [P] block (reads only)
    orchestrator.py    handle_chat: floor → scope → route → handlers → RAG →
                       grounding → validation → receipts (fail-open)
  rag/
    retrieval.py       condition scoping (registry-driven resolve_scope with
                       static fallback) + keyword-fallback retrieval
    prompt.py          system prompt (grounding contract) + COMPACTED_CONTEXT_JSON
  grounding/claims.py  PURE marker parse/verify, strip_markers (off|log|enforce)
  knowledge/
    mcp_parser.py      docx → sections → chunks (512 Master Condition Profiles)
    registry.py        cached keyword index (word-boundary, stoplist), engine-
                       code map T2DM→MC001 / HTN→MC051 / CAD→MC052, fail-open
  drugs/service.py     drug-info intent extraction + lookup over drug_reference
                       (250K medicines); deterministic validator-safe replies
  llm/                 LLMProvider + ToolCallingProvider protocols, tools.py
                       (provider-neutral tool vocabulary), FakeProvider,
                       agnostic providers
                       (OpenAI-compatible + Anthropic, pure httpx, env-selected)
  coredata/service.py  reads over Flyway core tables (documents w/ family
                       consent, vitals, lifestyle) + lifestyle_log tracker WRITE
  models/coredata.py   partial external-table mappings (PG enums bound with
                       create_type=False; sqlite variants for tests)
  chat/abilities.py    PURE parsers: document/tracker/metric/summary/suggestion
  chat/data_handlers.py deterministic ability handlers (run in SAVEPOINTs)
  charts/svg.py        deterministic SVG line/bar charts (visual payload)
  i18n/language.py     script-range detection + LLM reply-language directive
                       (the no-sidecar fallback; NO word lists or templates)
  translate/service.py English-pivot via the translator sidecar: detect →
                       to English → pipeline → back, digit-checked, fail-open
translator/            self-hosted sidecar (own Dockerfile, second Railway
                       service): IndicTrans2 + IndicXlit + IndicLID (all MIT)
  documents/service.py chat uploads: Davi touches NO document bytes/rows —
                       Spring's flow owns S3 + unclassified_files; this only
                       submits the mhn-ai processing run (verified contract:
                       POST /v1/document-processing-runs, bearer
                       MHN_SERVICE_TOKEN; fail-open; job_runs bookkeeping)
  telemetry.py         stdlib Prometheus exposition (/metrics); the metric
                       registry `_ALL` is HAND-MAINTAINED — a Counter declared
                       elsewhere renders nowhere
  models/feedback.py   reader verdicts on turns (no FK: must outlive the chat)
  models/review.py     clinician roster + append-only access audit
  api/v1/              health, pedigree, insights, chat (+ /chat/upload,
                       /chat/sessions history endpoints), feedback, review,
                       profile, schemas
evals/scenarios.json   safety-invariant scenarios (scripts/run_evals.py + pytest)
scripts/               seed_rules_templates, seed_synthetic, ingest_knowledge,
                       ingest_mcp_corpus, ingest_drugs, nightly_sweep, run_evals
knowledge/             3 synthetic condition files (T2DM, HTN, CAD) — unit tests
                       only; the real corpus is ingested from the MCP docx folder
tests/                 unit (aiosqlite) + pg-marked; tests/golden/artifacts.json
```

## Key invariants (enforced, tested)

- **Purity**: `insights/core.py`, `grounding/claims.py`, `chat/memory.py` are
  stdlib-only and side-effect free. Keep DB/LLM out of them.
- **Reads never compute**: only `recompute_insights` (after pedigree writes and
  in the nightly sweep) creates artifacts. `GET /insights` and the data-query
  handler only serve stored rows.
- **Stable identity**: an artifact's `content_hash` covers
  (facts_used, fired_rules, tier, template+version, body); identical inputs →
  no new row (idempotency test + committed golden snapshot).
- **Triage is a floor** that runs before scope/route/LLM; downstream may raise
  the level, never lower it. Emergencies are answered deterministically (no LLM).
- **One vocabulary**: compaction detects flags with the SAME triage tables.
- **Fail open**: grounding/validation/receipts/compaction/provider crashes →
  safe reply + WARNING, never an exception to the caller.
- **No PHI in logs / receipts**: receipts store SHA-256 of the message only.
- **Identity privacy**: the underlying model/provider is never disclosed.
  Model/provider questions route to the canned identity reply (no LLM); the
  system prompt forbids naming providers; the validator bans provider names in
  generated text (`provider-leak`) — word-boundaried so SGPT/claudication and
  "what model of BP monitor" stay on their normal paths.
- **Drug path is deterministic**: drug-information questions are answered from
  drug_reference (never the LLM), only at NONE risk (the triage floor wins),
  gated by NON_DRUG_TERMS, with ordered candidate windows for reproducibility.
- **Registry keyword matching is guarded**: word boundaries, plural tolerance,
  ≥4-char keywords (3-char only for ALL-CAPS abbreviations, matched
  case-sensitively — "ARM" ≠ "arm"), stoplist for everyday words, and
  parenthetical qualifiers like "(child)" never become keywords.

## Current state

Original build phases 0–7 complete, plus `project_docs/implementation-plan.md`
phases 0–4 (Tasks 1–11, 13–28). **Task 12 — retiring the legacy regex chain —
is deliberately blocked**; see `project_docs/handover.md`.

Suite green on aiosqlite (1718 tests, clean under three random seeds); the
`pg`-marked reversibility + coexistence checks pass on a local Postgres 16 with
pgvector. `scripts/run_evals` is 17/17 on BOTH engines. ruff + pyright clean.
All clinical content is DRAFT.

Docker is not available on the original dev machine, so `pg` tests were run
against a Homebrew Postgres 16 with a source-built pgvector — see the
`local-dev-postgres` memory for the exact setup.

### Two chat engines

`CHAT_ENGINE=legacy|agentic`, defaulting to **legacy**. Legacy is the regex
handler chain; agentic lets the model orchestrate the same abilities as tools.
The triage floor, scope guard, emergency directive, canned conversational
replies and the drug-combination refusal all run in a **shared prologue** ahead
of the engine branch, and validation/grounding run behind both.

**If you add a deterministic, safety-relevant handler, put it in the shared
prologue, not in the legacy chain.** A drug-interaction refusal sat at step 5
inside the legacy branch and the agentic engine bypassed it entirely — the
model answered interaction questions from its own weights. The other step-4 and
step-5 handlers have not been audited for the same problem.

### Documentation for agents

`project_docs/` carries the working record: `handover.md` (resume here),
`memory.md` (invariants), `implementation-log.md` (what was decided and why),
`findings.md` + `findings-phase-4.md` (review findings, including refuted
ones), and `decisions-needed.md` (autonomous calls awaiting review).

## Gotchas

- `mcp_chunks.embedding` is `Vector(1024)` on PG, JSON variant on sqlite; the
  Phase-1 migration hardcodes `Vector(1024)` and `CREATE EXTENSION vector`.
  Autogenerate against sqlite emits a spurious embedding type-diff — drop it.
- Adding a model column? Autogenerate against a sqlite scratch DB, then hand-fix
  (imports, the embedding false-diff), and re-run the coexistence check.
- `sqlite` needs `PRAGMA foreign_keys=ON` for cascade tests (set in conftest).
- A new `db/flyway/V*__davi_*.sql` MUST register its tables in
  `tests/test_flyway_parity.py::FLYWAY_TABLES` or that test fails. The whole
  suite runs on Alembic-built schema, so without the parity check a drifted
  column passes every test here and fails only in production.
- **Flyway version numbers are a SHARED namespace with mhn-spring, and nothing
  in this repo can see their chain.** Davi staged V7-V10 while mhn-spring used
  those same numbers for `medical_history`, `medical_history_date_order`,
  `period_pause_and_pregnancy` and `ai_name_check` — four migrations that could
  never have been applied. Everything after V6 is now consolidated into
  `V20__davi_chat_platform.sql`. Before adding another, check mhn-spring's head
  and go above it; `test_davi_migrations_do_not_collide_with_mhn_spring` does
  this automatically when the sibling checkout exists (or set
  `MHN_SPRING_PATH`), and skips where it does not.
- Prompt caching: `system` is `str | Sequence[str]`, element 0 is the
  byte-identical cached prefix. Append per-turn text with `append_directive()`,
  never `system + x`. The prefix is ~2,541 tokens and the system rules ALONE
  (~850) are under the 1024 minimum — the tool schemas are what carry it over.

## Task Lifecycle

For substantial implementation work, follow this general lifecycle:

1. Understand and review the existing code.
2. Identify drawbacks, risks, and affected areas.
3. Create an implementation plan.
4. Execute the approved implementation plan using `.claude/execution-rules.md`.
5. Independently review the resulting implementation using `.claude/review-rules.md`.
6. Fix legitimate review findings.
7. Run final verification and inspect the git diff.

Do not skip directly from a high-level requirement to implementation when the task requires substantial architectural or code changes.

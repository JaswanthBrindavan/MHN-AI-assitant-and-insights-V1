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

## Database coexistence (important)

This backend shares the existing **MHN/Davi production database**
(`db/existing_schema.sql`): a Flyway-managed core (`"user"`, `family_connect`,
`medicine_*`, `vital_reading`, …) plus a separate `ai_*` Alembic subsystem
(`ai_alembic_version`, `ai_processing_runs`, …).

Rules we follow (do not break these):

- Our tables use **plain `uuid` `user_id` columns with NO FK** to `"user"`
  (matching `ai_processing_runs`). FKs among our OWN tables are fine
  (`pedigree_conditions.consent_grant_id`, `insight_artifacts.superseded_by`,
  the `conversation_*` cascade).
- Our Alembic chain uses the **default `alembic_version`** table. The `"user"`
  table is in ORM metadata (a partial read-only `User` model, for seeding/reads)
  but **excluded from migrations** via `EXTERNAL_TABLES` + `include_object` in
  `app/alembic/env.py`.
- Migrations are verified two ways: reversibility on a fresh DB, and coexistence
  (apply on top of a full load of `db/existing_schema.sql`).

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
    retrieval.py       condition scoping + keyword-fallback retrieval
    prompt.py          system prompt (grounding contract) + COMPACTED_CONTEXT_JSON
  grounding/claims.py  PURE marker parse/verify, strip_markers (off|log|enforce)
  llm/                 LLMProvider protocol, FakeProvider, OllamaProvider, factory
  api/v1/              health, pedigree, insights, chat, schemas
scripts/               seed_rules_templates, seed_synthetic, ingest_knowledge,
                       nightly_sweep
knowledge/             3 synthetic condition files (T2DM, HTN, CAD)
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
- **Fail open**: grounding/validation/receipts/compaction crashes → safe reply +
  WARNING, never an exception to the caller.
- **No PHI in logs / receipts**: receipts store SHA-256 of the message only.

## Current state

Phases 0–7 complete. Suite green on aiosqlite; the `pg`-marked reversibility +
coexistence checks pass on a local Postgres 16 with pgvector. Coverage ~91%
(gate ≥80%). ruff + pyright clean. All clinical content is DRAFT.

Docker is not available on the original dev machine, so `pg` tests were run
against a Homebrew Postgres 16 with a source-built pgvector — see the
`local-dev-postgres` memory for the exact setup.

## Gotchas

- `mcp_chunks.embedding` is `Vector(1024)` on PG, JSON variant on sqlite; the
  Phase-1 migration hardcodes `Vector(1024)` and `CREATE EXTENSION vector`.
  Autogenerate against sqlite emits a spurious embedding type-diff — drop it.
- Adding a model column? Autogenerate against a sqlite scratch DB, then hand-fix
  (imports, the embedding false-diff), and re-run the coexistence check.
- `sqlite` needs `PRAGMA foreign_keys=ON` for cascade tests (set in conftest).

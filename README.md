# Davi Health AI — backend

A production-grade healthtech backend for Indian families, in two halves:

1. **Deterministic insights engine** — family-history (pedigree) risk patterns →
   clinician-gated template insights → versioned, reproducible artifacts. **No
   LLM anywhere in this half.**
2. **Clinical chatbot chassis** — deterministic red-flag triage floor → scoped
   RAG retrieval → LLM answer with mechanical claim-grounding → validation →
   auditable receipts, with structured context compaction for long chats.

**This is decision support, never diagnosis.** That framing is enforced in code
(banned-phrase validator, mandatory `{not_a_diagnosis}`/`{next_step}` template
sections, emergency-floor-before-LLM ordering), not just in comments.

> All clinical constants (phrase tables, thresholds, seed rules, templates) ship
> as **DRAFT — pending clinician sign-off**.

## Stack

Python 3.11+ · FastAPI (async) · SQLAlchemy 2 async (asyncpg) · Alembic ·
PostgreSQL 16 + pgvector · Docker Compose · pytest + aiosqlite (with a
`pg`-marked subset for real Postgres) · ruff + pyright. The LLM sits behind an
`LLMProvider` protocol (`OllamaProvider` for real use, deterministic
`FakeProvider` for tests). **Nothing in the test suite needs a live LLM,
network, or GPU.**

### Coexistence with the existing MHN/Davi database

This backend runs **alongside the existing production schema** (see
`db/existing_schema.sql`): the Flyway-managed core (`"user"`, `family_connect`,
`medicine_*`, …) plus an existing `ai_*` Alembic subsystem. Accordingly:

- Our tables store `user_id` as a plain `uuid` with **no FK** to `"user"` (the
  same convention the existing `ai_processing_runs` table uses).
- We use the **default `alembic_version`** table; the existing subsystem keeps
  `ai_alembic_version`. Our migrations only ever create our own tables — the
  `"user"` table is excluded (`EXTERNAL_TABLES`).
- A standalone deployment (empty Postgres) works too: there is no `"user"`
  table, and `seed_synthetic` skips fake-user creation when it is absent.

## Run

### Docker Compose

```bash
cp .env.example .env
docker compose up --build          # api on :8000, pgvector/pg16 on :5432
# the api container runs `alembic upgrade head` on start
```

### Local (without Docker)

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

export DATABASE_URL=postgresql+asyncpg://davi:davi@localhost:5432/davi
export ALEMBIC_DATABASE_URL=postgresql+psycopg2://davi:davi@localhost:5432/davi
alembic upgrade head
uvicorn app.main:app --reload
```

## Seed & ingest

```bash
python -m scripts.seed_rules_templates     # 6 DRAFT rules + 3 templates
python -m scripts.seed_synthetic           # 3 synthetic users (every rule branch)
python -m scripts.ingest_knowledge knowledge   # 9 synthetic knowledge chunks (tests)
```

### Clinically-validated knowledge base

The production knowledge base is built from two validated sources:

```bash
# 512 Master Condition Profiles (docx) → condition_registry + mcp_chunks
python -m scripts.ingest_mcp_corpus "/path/to/MHN_Master_Condition_Profiles/Documents"

# Merged Indian medicines database (~250K rows) → drug_reference
python -m scripts.ingest_drugs "/path/to/merged_medicines.csv"
```

This yields ~511 conditions (with AKA aliases driving retrieval scoping),
~17,000 knowledge chunks, and ~250,561 medicines powering the deterministic
drug-information path (`"what is metformin used for?"`, `"side effects of
augmentin 625"`). Legacy engine codes map to profiles (T2DM→MC001, HTN→MC051,
CAD→MC052) so pedigree-scoped retrieval reaches the validated corpus.

`scripts/nightly_sweep.py` recomputes all users and hard-purges pedigree
conditions soft-deleted more than 30 days ago, writing a `job_runs` row.

## Curl walkthrough

```bash
UID_HDR='X-User-Id: 33333333-3333-3333-3333-333333333333'    # synthetic user C
curl -s localhost:8000/health

# Insights (active only; held_for_review excluded)
curl -s localhost:8000/api/v1/insights -H "$UID_HDR"

# Chat — deterministic emergency floor (no LLM)
curl -s localhost:8000/api/v1/chat -H "$UID_HDR" -H 'Content-Type: application/json' \
  -d '{"message":"I cant breathe"}'

# Chat — scoped RAG + grounding
curl -s localhost:8000/api/v1/chat -H "$UID_HDR" -H 'Content-Type: application/json' \
  -d '{"message":"what should I know about diabetes and blood sugar?"}'
```

## Test

```bash
pytest                       # unit suite on aiosqlite (default: -m "not pg")
pytest --cov=app             # with the >=80% coverage gate
ruff check . && pyright      # lint + types

# real-Postgres subset (needs a live PG with pgvector):
TEST_ALEMBIC_URL=postgresql+psycopg2://davi:davi@localhost:5432/davi pytest -m pg
```

Golden artifact snapshot lives at `tests/golden/artifacts.json`; regenerate with
`GOLDEN_UPDATE=1 pytest tests/test_engine_api.py::test_golden_artifacts_snapshot`.

The real `OllamaProvider` can be smoke-tested manually — see
[`docs/ollama_smoke.md`](docs/ollama_smoke.md).

## API surface (all under `/api/v1`, JWT-guarded when `AUTH_ENABLED=true`)

| Method | Path | Notes |
| --- | --- | --- |
| `PUT` | `/pedigree` | Upsert slots + conditions; first write records a `family_risk_analysis` consent grant; recompute runs after write |
| `GET` | `/pedigree` | Current slots + conditions (soft-deleted excluded) |
| `DELETE` | `/pedigree/conditions/{id}` | Soft-delete + recompute |
| `GET` | `/insights` | Active artifacts only (`held_for_review` excluded) |
| `POST` | `/chat` | `{message}` → `{response_message, risk_level, recommended_action, provenance, grounding, session_id}`. Drug-information questions ("side effects of dolo 650") get a deterministic reply from `drug_reference` — never the LLM — with the mandatory medication safety note; the red-flag floor always wins over the drug path. |
| `GET` | `/health` | Liveness |

Every endpoint taking a `user_id` resolves it against the token identity and
returns **403** on mismatch (object-level authorization). When
`AUTH_ENABLED=false`, identity comes from the `X-User-Id` header (dev only).

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://davi:davi@localhost:5432/davi` | Async app engine |
| `ALEMBIC_DATABASE_URL` | `postgresql+psycopg2://…` | Sync engine for migrations |
| `AUTH_ENABLED` | `false` | Enable HS256 JWT auth (`sub` = user UUID) |
| `JWT_SECRET` / `JWT_ALGORITHM` | `change-me` / `HS256` | JWT signing |
| `LLM_PROVIDER` | `fake` | `fake` \| `openai_compatible` \| `anthropic` \| `ollama` — model/cloud agnostic |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | *(empty)* | Any OpenAI-compatible endpoint (OpenAI, Groq, Together, vLLM, LM Studio…) or the Anthropic API |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | `http://localhost:11434/v1` / `llama3.1` | Legacy Ollama alias (OpenAI-compatible `/v1`) |
| `REPLY_LANGUAGE` | `auto` | `auto` mirrors the user's detected language |
| `LLM_PROMPT_VERSION` | `v1` | Recorded on receipts |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` | *(empty)* / *(empty)* / `1024` | Optional embeddings; unset → NULL embeddings + keyword retrieval |
| `GROUNDING_MODE` | `log` | `off` \| `log` \| `enforce` |
| `PIPELINE_VERSION` | `1` | Stamped onto artifacts |

## Chat data-abilities (deterministic, no LLM)

The chat understands and answers these directly from the shared MHN database:

- **Documents** — "find my latest blood report", "when was my father's last
  test done?" (family documents respect accepted connections, file-share
  consent, and privacy flags)
- **Tracker adds** — "I had 3 cups of coffee today", "I smoked 2 cigs
  yesterday" → written to `lifestyle_log` with backdating
- **Metric pulls** — "what's my latest HbA1c / blood pressure / weight" from
  vitals, body measurements, and extracted lab-report content
- **Health summaries** — "health summary for the week/month/year" with a
  rendered SVG chart (`visual` payload: declarative spec + self-contained SVG)
- **MCP suggestions** — "tips for my diabetes" served verbatim from the
  clinically validated condition profiles, with structured `citations`

Multilingual: language is detected per message (Indic scripts + romanized
Hindi); deterministic safety replies are localized (DRAFT), red-flag triage
includes Hindi/Hinglish phrases, and the LLM is instructed to reply in the
user's language. RAG answers return structured `citations` mapped from the
grounding markers.

## Test console (UI) + demo accounts

A self-contained test console (vanilla HTML/JS, no build step) is served at
`http://localhost:8000/` by the API itself. It offers a persona switcher,
one-click quick tests for every chat path, risk/provenance/language chips,
citation display, inline SVG charts, and Pedigree/Insights tabs.

```bash
python -m scripts.seed_demo_users   # seeds the 6 demo accounts below
uvicorn app.main:app                # then open http://localhost:8000/
```

| Persona | UUID prefix | What they exercise |
| --- | --- | --- |
| **Asha** | `1111…` | One-parent diabetes + hypertension pedigree |
| **Bharat** | `2222…` | Both-parents diabetes (worth_discussing tier) |
| **Chandra** | `3333…` | Early-onset + vertical-chain diabetes, premature CAD |
| **Deepa** | `4444…` | Rich data: BP/sugar vitals series, lab report with HbA1c 6.1%, week of lifestyle logs, connected father |
| **Eshan** | `5555…` | Deepa's father — shares his lipid report (plus one private doc that must stay hidden) |
| **Farah** | `6666…` | Empty account (fresh-user experience) |

All accounts and every data point are synthetic.

## Eval harness

```bash
python -m scripts.run_evals          # 15 safety scenarios through the full pipeline
```

Scenario specs live in `evals/scenarios.json` (also run as part of pytest).

## Safety rules (enforced in code)

- Never outputs "you have X", disease probabilities as numbers, or "your
  medication is causing X" (banned-phrase validator).
- Any medication-touching reply says: don't stop/change a dose on your own —
  discuss with the prescriber.
- Deterministic red-flag triage runs **before** any keyword gate, handler, or
  LLM, and is a severity **floor** (downstream may raise it, never lower it).
- Every insight template must contain `{not_a_diagnosis}` and `{next_step}`; the
  renderer raises `TemplateContractError` otherwise.
- Sensitive rules → `held_for_review`, never auto-surfaced.
- Safety layers **fail open**: a crash in grounding/compaction/receipts/
  validation degrades to the deterministic safe reply and logs a WARNING.
- **No PHI in logs.** Receipts store SHA-256 hashes of messages, never raw text.
- Synthetic data only.

## Production notes

- The `consent_ledger` table is **append-only**. Revoke `UPDATE`/`DELETE` on it
  from the application DB role in production; the app has no code path that
  updates or deletes rows there.

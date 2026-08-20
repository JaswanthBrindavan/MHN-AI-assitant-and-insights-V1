# Production integration — mhn-spring · mhn-ai · mhn-react

How this Davi AI service plugs into the existing production stack. Every
contract below was verified against the production source (paths cited).

## The stack

| Repo | Role | Relationship to Davi |
| --- | --- | --- |
| `mhn-spring` | Java/Spring API + owner of the shared PostgreSQL schema (Flyway) | Davi reads its tables directly (and writes only `lifestyle_log`) |
| `mhn-ai` | Per-document AI: classify → file → extract labs → per-document insights | Sibling service. Davi READS its output (`content.ai` envelopes, `thp_age_range`) and never touches its `ai_*` tables |
| `mhn-react` | TanStack Start frontend, BFF pattern (browser → own `/api/*` routes → Spring) | Calls Davi server-side from new BFF routes |

## Auth contract (verified: `app/security/JwtService.java`)

- Session tokens are **HS512** JWTs; `sub` = user UUID.
- The shared `JWT_SECRET` is **Base64-decoded before HMAC**
  (`Decoders.BASE64.decode(jwtSecret)` → `hmacShaKeyFor`). Davi mirrors this
  via `JWT_SECRET_BASE64=true` (default) — set the SAME `JWT_SECRET` env value
  Spring uses; Davi decodes it identically.
- HS256 exists in Spring only for short-lived OTP/OAuth challenge tokens —
  never accept those as sessions (they carry `type` claims, not a user `sub`).
- Alternative server-to-server path (the Spring↔mhn-ai `AI_TOKEN` /
  `MHN_SERVICE_TOKEN` pattern): set `SERVICE_TOKEN` (≥32 chars) and call Davi
  with `Authorization: Bearer <SERVICE_TOKEN>` + `X-User-Id: <uuid>` after the
  caller (Spring or the React BFF) has authenticated the user itself.
  Constant-time compared. Keep Davi off the public internet in this mode.

Davi env for production:

```bash
AUTH_ENABLED=true
JWT_SECRET=<same value as Spring>   # Base64 string; Davi decodes it
JWT_ALGORITHM=HS512
JWT_SECRET_BASE64=true
# optional server-to-server:
SERVICE_TOKEN=<random ≥32 chars>
```

## Triggering mhn-ai from chat uploads (verified: mhn-ai `app/api/v1/runs.py`)

Davi's `POST /api/v1/chat/upload` submits documents to mhn-ai's processing
API. The contract, verified against the mhn-ai repo:

- **Endpoint**: `POST {MHN_AI_BASE_URL}/v1/document-processing-runs` → `202`
  `{run_id, created_at, items: [{document_id, item_id, status ("queued" |
  "failed"), outcome, error_code}]}`. Callers poll
  `GET /v1/document-processing-runs/{run_id}`.
- **Auth**: `Authorization: Bearer <token>` where the token is mhn-ai's
  `MHN_SERVICE_TOKEN` (HTTPBearer + `secrets.compare_digest`; mhn-ai does no
  user-level authorization — `requested_by_user_id` is audit-only).
- **Payload**: `{"documents": [{"document_id": <unclassified_files id>,
  "intended_section": null}], "requested_by_user_id": "<uuid>"}` — the unit
  is an `unclassified_files` row; `intended_section` stays null for a global
  (chat) upload.
- **Bytes**: the worker downloads `unclassified_files.filepath` from
  Spring's S3 bucket. Davi does NOTHING with documents itself — no bytes, no
  rows: a chat upload goes through Spring's existing upload flow (S3 + the
  `unclassified_files` row) exactly like every other upload, and Davi's
  `POST /api/v1/chat/upload` then takes the resulting `document_id`, checks
  the row belongs to the caller (READ only), and submits the run — the same
  call Spring makes.

Davi env: `MHN_AI_BASE_URL` (keep it on the private network),
`MHN_AI_TOKEN` (same value as mhn-ai's `MHN_SERVICE_TOKEN`).

## Schema ownership (verified: `railway.toml`, `V4__ai_tables.sql`)

Flyway in `mhn-spring` owns **all** production schema — since 2026-08-06 even
the `ai_*` tables (mhn-ai's Alembic is frozen at `b6d1f8a3c209` and builds
local/test DBs only). Davi follows the same rule:

- **Adopt [`db/flyway/V6__davi_ai_tables.sql`](../db/flyway/V6__davi_ai_tables.sql)**
  into `mhn-spring/src/main/resources/db/migration/` — it creates all 17 Davi
  tables (uuid `user_id`, no FK to `"user"`, pgvector for `mcp_chunks`).
- Davi's Alembic chain (version table **`davi_alembic_version`**, mirroring
  `ai_alembic_version`) is for local/test databases only from now on.

## Reading mhn-ai's output (verified: `app/services/assembly.py`)

Lab extractions live in `reports.content` under a namespaced `ai` key:

```
content.ai = {
  schema_version: "2.1", state: "classified"|"complete"|"failed",
  document_id, classification: {section, title, confidence},
  extraction: { results: [ {test_name, value (verbatim string), unit,
      reference_range, observed_date, source_context,
      value_numeric, abnormal_flag ("low"|"normal"|"high"|null),
      range_source, flagged_against, normalized_value, normalized_unit} ],
      report_date, patient_age, patient_gender },
  insights: {insights[], summary, disclaimer} | null }
```

Davi's readers (metric pulls, health snapshot, lab values) handle this shape:
`test_name` is matched, `value_numeric` preferred, and production's
Python-computed `abnormal_flag` is surfaced (e.g. "126 (high)"). The legacy
demo shape (`{"tests": [...]}`) still works. `abnormal_flag`/`flagged_against`
are authoritative — never re-judge a value against `reference_range` when
`range_source == "ideal_range"`.

Scans have no name column — listing titles come from
`content.ai.classification.title` (Davi does the same).

## Family consent (verified: `FileServiceImpl.assertCanRead`)

Read access to a family member's file requires ALL of:
1. accepted `family_connect` row, with the **owner-side** read grant on:
   `req_read` when the owner sent the request, `acc_read` when they accepted
   (legacy `*_file_share` columns are the fallback when the new ones are NULL);
2. the file not `private`;
3. no `file_access_exclusions` row for (viewer, resource_type, resource_id).

Davi's `resolve_family_member` + `latest_documents(viewer_id=…)` implement
exactly this.

## Reference ranges (verified: `ideal_ranges.resolve`, `thp_age_range`)

`traditional_health_parameters` + `thp_age_range` (graduated
min/low_danger/low_warn/ideal/high_warn/high_danger/max, age-banded, with
`thp_alternate_units` conversions) are the curated ideal ranges. Davi's
value-check reads them (age from `user.dob`) and maps severity → escalation;
DRAFT constants are the fallback when no THP matches.

## Writes Davi makes to shared tables

Only `lifestyle_log` (tracker adds). `coalesce_bucket` stays NULL — the
partial-unique index (`WHERE coalesce_bucket IS NOT NULL`) ignores our rows,
and Spring's nightly reconciler (03:15, `TRACKING_ZONE`) folds them into the
rollup tables. Everything else is read-only from Davi.

## Frontend integration (from the mhn-react map)

- **BFF route** `src/routes/api/ai/chat.ts` using `chain(withRateLimit(), withAuth())`;
  create `src/common/davi-server.ts` (a `spring-server.ts` sibling; `DAVI_API_URL`
  env) and forward the session Bearer token — or clone the catch-all proxy
  `src/routes/api.spring.$.ts` as `/api/davi/$`.
- **Chat module** `src/modules/chat/{types,hooks}.ts` + page route
  `src/routes/_authenticated/chat.tsx` + a NavNode in `src/components/Sidebar.tsx`.
- Davi's response contract: `{response_message, risk_level, recommended_action,
  provenance, citations, trace, visual, language, session_id}`. Render
  `trace` as the collapsible thinking chain, `visual.svg` inline, risk chips
  via the existing badge/flagColor mapping (rose/amber/emerald).
- **Insights feed**: BFF `src/routes/api/ai/insights.ts` → Davi `GET /api/v1/insights`;
  render with `CollapsibleCard` + the Risk-Patterns card treatment from
  `src/modules/files/insights.tsx`; disclaimer-in-payload convention holds.
- `apiFetch` buffers whole bodies — Davi replies are non-streaming JSON, which
  fits; the client-side typewriter supplies the streaming feel.
- Pass AI text through `useTransliterate` like the existing insight components.

## Why Davi has no AWS code (deliberate)

The other services use AWS for exactly two things, and Davi needs neither:

| AWS usage | Who | Why Davi doesn't |
| --- | --- | --- |
| **S3** — file bytes, presigned upload/download (`S3StorageService`, `S3PostPolicySigner`) | mhn-spring owns the bucket; mhn-ai fetches source objects + relocates keys on filing | Davi never touches file BYTES. It reads the **extracted JSON** (`content.ai`) that mhn-ai already produced from those bytes — the S3 `filepath` is an opaque key used at most as a fallback display name |
| **SQS** — async document-processing queue | mhn-ai (worker long-polls) | Chat is synchronous request/response; the nightly sweep is a cron job. Nothing to queue |

**Opening a document from a chat reply** uses the EXISTING production flow, no
Davi involvement: Davi's document answers return
`provenance.documents[{kind, id, created_at}]` — the frontend/BFF takes those
ids to Spring's `GET /files/{type}/{id}/url` (presigned GET, same
authorization as the file bytes). Davi never mints URLs.

This is a security posture, not a shortcut: the AI chat service holds **no AWS
credentials at all** — its blast radius is the database permissions it runs
with. If full-document Q&A is ever wanted ("read my actual PDF"), prefer
reusing `content.ai.extraction` + `insights` (already derived from the
document); only if that's insufficient add read-only `GetObject` creds then.

## Deployment (matching mhn-ai's Railway pattern)

One container, uvicorn on `$PORT`, healthcheck `/health`, HTTP/1.1 (Spring's
`AiClient` pins HTTP_1_1 — same applies if Spring ever calls Davi). Nightly
`scripts/nightly_sweep.py` as a cron job. Needs `DATABASE_URL` (asyncpg,
shared DB), the LLM + embedding env, and the auth env above.

## Full local-parity on Railway (Haiku + hybrid search)

To run on Railway EXACTLY like the local setup (Anthropic Haiku answers +
hybrid BM25⊕vector retrieval), deploy TWO services from this repo:

**1. `davi-embeddings` — Ollama with the model baked in**
- New Railway service → Deploy from GitHub repo → select THIS repo (yes, the
  same repo twice — one repo, two services, like mhn-ai's api+worker).
- Settings → **Config-as-code → path = `railway.embeddings.toml`** — this is
  required, not optional: config-as-code overrides dashboard settings, so
  without it the service inherits the repo-root `railway.toml` (the API's
  Dockerfile and a `/health` healthcheck Ollama doesn't serve).
- Variables → add **`PORT=11434`** — Railway healthchecks the port named by
  PORT (auto-generated when unset), while Ollama listens on 11434; without
  this the deploy loops on "service unavailable".
- **No public domain** (private-only). No other env needed (model + settings
  are baked into the image at build time).
- **No Railway Volume** on this service: a volume mounted at `/root/.ollama`
  starts empty and shadows the baked model (runtime log `total blobs: 0`,
  embeds fail model-not-found). The image stores models at `/bundled-models`
  to be immune, and the build fails if the model didn't persist — but there is
  still no reason to attach one.
- Davi reaches it at `http://davi-embeddings.railway.internal:11434/v1`
  (swap in the actual service name).

**2. `davi-api` — this service** (default `Dockerfile`, `railway.toml`), with
the standard env plus:

```bash
EMBEDDING_BASE_URL=http://davi-embeddings.railway.internal:11434/v1
EMBEDDING_MODEL=qwen3-embedding:0.6b
EMBEDDING_DIM=1024
```

**3. One-time ingest WITH embeddings** (from a dev machine — its GPU embeds
the 16,637 chunks in ~an hour; the vectors are stored in pgvector, so the
Railway Ollama only ever embeds short queries afterwards):

```bash
DATABASE_URL=postgresql+asyncpg://…shared-db… \
EMBEDDING_BASE_URL=http://localhost:11434/v1 \
EMBEDDING_MODEL=qwen3-embedding:0.6b EMBEDDING_DIM=1024 \
python -m scripts.ingest_mcp_corpus "/path/to/MCP/Documents"

DATABASE_URL=postgresql+asyncpg://…shared-db… \
python -m scripts.ingest_drugs "/path/to/merged_medicines.csv"
```

Invariant: **ingest-time and query-time embedding model must be identical**
(qwen3-embedding:0.6b, native 1024 dims). Changing the model means
re-ingesting. If the embedding service is ever down, retrieval fail-opens to
keyword mode — degraded relevance on colloquial symptom phrasings, never an
error.

**Console caveat on a shared DB:** the test console requires
`AUTH_ENABLED=false` (X-User-Id header identity), which on the shared
production database would let anyone impersonate any user. Keep
`AUTH_ENABLED=true` on Railway and test via the API with real session JWTs
(or the SERVICE_TOKEN path); use the console locally.

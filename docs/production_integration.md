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

## Schema ownership (verified: `railway.toml`, `V4__ai_tables.sql`)

Flyway in `mhn-spring` owns **all** production schema — since 2026-08-06 even
the `ai_*` tables (mhn-ai's Alembic is frozen at `b6d1f8a3c209` and builds
local/test DBs only). Davi follows the same rule:

- **Adopt [`db/flyway/V5__davi_ai_tables.sql`](../db/flyway/V5__davi_ai_tables.sql)**
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

## Deployment (matching mhn-ai's Railway pattern)

One container, uvicorn on `$PORT`, healthcheck `/health`, HTTP/1.1 (Spring's
`AiClient` pins HTTP_1_1 — same applies if Spring ever calls Davi). Nightly
`scripts/nightly_sweep.py` as a cron job. Needs `DATABASE_URL` (asyncpg,
shared DB), the LLM + embedding env, and the auth env above.

# Davi Health AI — Architecture

**Status:** as-built, verified against the code on branch `praveen-mhn` (commit `fee14d4`).
**Scope:** this repo (`MHN-AI-assitant-and-insights-V1`) plus its contracts with the
production stack (`mhn-spring`, `mhn-ai`, `mhn-react`).
**Companion document:** [`drawbacks.md`](./drawbacks.md) — gap analysis against an
August.ai-class conversational health assistant.

---

## 1. One-paragraph summary

Davi is a **decision-support** healthtech backend, not a diagnostic one, and that
constraint is enforced in code rather than in policy documents. It has two halves that
share a database and an auth model but share no logic: a **deterministic insights
engine** that turns family history into reproducible, hash-identified insight artifacts
with no LLM anywhere, and a **chat chassis** in which the LLM is the *last* component in
a pipeline of deterministic gates — red-flag triage, scope guard, intent routing, data
abilities, drug lookup — each of which can answer without ever reaching a model. Every
safety layer fails open to a canned safe reply rather than raising.

---

## 2. System context

```mermaid
graph TB
    subgraph client["Clients"]
        REACT["mhn-react (BFF)<br/>browser → /api/* → Spring/Davi"]
        CONSOLE["ui/index.html<br/>dev test console"]
    end

    subgraph davi["THIS REPO — davi-api"]
        API["FastAPI<br/>/api/v1/*"]
        INS["Insights engine<br/>(no LLM)"]
        CHAT["Chat chassis"]
    end

    subgraph side["Sidecars (own services)"]
        TRANS["translator/<br/>IndicTrans2 + IndicXlit + IndicLID"]
        EMB["davi-embeddings<br/>Ollama qwen3-embedding"]
    end

    subgraph prod["Production stack"]
        SPRING["mhn-spring<br/>Java API · Flyway owns ALL schema · S3"]
        MHNAI["mhn-ai<br/>classify → file → extract → insights"]
    end

    LLM["LLM provider<br/>Anthropic / OpenAI-compatible / fake"]
    DB[("Shared PostgreSQL 16<br/>+ pgvector")]

    REACT --> API
    CONSOLE --> API
    API --> INS
    API --> CHAT
    CHAT --> TRANS
    CHAT --> EMB
    CHAT --> LLM
    CHAT -->|"POST /v1/document-processing-runs"| MHNAI
    INS --> DB
    CHAT --> DB
    SPRING --> DB
    MHNAI --> DB
    SPRING -->|"file bytes"| MHNAI
```

**Davi holds no AWS credentials.** It never touches file bytes; it reads the extracted
`content.ai` JSON that mhn-ai already produced. Its blast radius is the database
permissions it runs with.

---

## 3. Layer map

```
app/
├── config.py            pydantic-settings — every knob is an env var
├── db.py                async engine (pool_pre_ping, pool_recycle=300) + get_db
├── auth.py              HS512 JWT (Base64-decoded secret) | SERVICE_TOKEN + X-User-Id
├── main.py              app factory, router wiring, dev console mount
│
├── api/v1/              health · pedigree · insights · chat · documents · schemas
│
├── insights/            ── HALF ONE: deterministic, no LLM ──
│   ├── constants.py     DRAFT clinical constants (onset midpoints, weights, copy)
│   ├── core.py          PURE stdlib: facts → 6 patterns → evaluate → render → hash
│   └── engine.py        recompute_insights — the ONLY DB-touching part
│
├── triage/red_flags.py  DRAFT phrase tables; the severity FLOOR
├── chat/                ── HALF TWO: the chassis ──
│   ├── orchestrator.py  handle_chat — the pipeline (854 lines)
│   ├── scope.py         off-topic decline
│   ├── router.py        deterministic intent routing
│   ├── abilities.py     PURE regex parsers (737 lines)
│   ├── data_handlers.py deterministic ability handlers (1380 lines)
│   ├── context.py       patient [P] block + personal health snapshot
│   ├── conversation.py  session/message persistence, compaction, context assembly
│   ├── memory.py        PURE compaction extractors + merge
│   ├── long_term.py     cross-session topic/flag memory
│   ├── validation.py    banned-phrase + escalation validator
│   └── replies.py       canned + safe replies (all validator-safe)
│
├── rag/                 retrieval (hybrid) · ranking (BM25+RRF+MMR) · embeddings
│                        · prompt (system prompt) · extractive (no-LLM answers)
├── grounding/claims.py  PURE marker parse/verify/strip
├── knowledge/           mcp_parser (docx → chunks) · registry (keyword index)
├── drugs/service.py     deterministic drug-info over drug_reference (~250K rows)
├── coredata/service.py  reads over Flyway tables + the ONE write (lifestyle_log)
├── documents/service.py mhn-ai processing-run trigger (no bytes, no rows)
├── health/              ranges.py (DRAFT constants) · reference.py (THP backend)
├── charts/svg.py        deterministic SVG line/bar charts
├── i18n/language.py     Unicode script-range detection + LLM language directive
├── translate/service.py English-pivot via the sidecar, digit-checked, fail-open
├── llm/                 LLMProvider protocol + agnostic httpx providers + fake
└── models/              common · core · rules · chat · knowledge · coredata · jobs
```

**Purity contract.** `insights/core.py`, `grounding/claims.py`, and `chat/memory.py`
are stdlib-only and side-effect free. No DB, no LLM, no randomness, no clock. They are
the parts that are 100%-testable and reproducible; keep them that way.

---

## 4. Half one — the insights engine

Family history in, versioned insight artifacts out. No model, no network, no randomness.

```mermaid
flowchart LR
    A["pedigree_conditions<br/>(user, slot, condition)"] --> B["assemble_facts<br/>PURE"]
    B --> C["ConditionFacts<br/>parents · grandparents<br/>min_parent_onset<br/>vertical_chain<br/>weighted_load"]
    C --> D["evaluate<br/>6 pattern predicates"]
    R["risk_rules<br/>(rules are DATA)"] --> D
    D --> E["aggregate → tier<br/>typical / worth_knowing<br/>/ worth_discussing"]
    E --> F["render_insight<br/>template contract"]
    T["insight_templates"] --> F
    F --> G["content_hash<br/>sha256"]
    G --> H{"same hash as<br/>live artifact?"}
    H -->|yes| I["no-op"]
    H -->|no| J["INSERT artifact<br/>supersede previous"]
```

### The six patterns

| `pattern_key` | Fires when |
|---|---|
| `parental_count` | ≥ *min* affected parents |
| `both_parents` | mother **and** father affected |
| `grandparent_count` | ≥ *min* affected grandparents |
| `early_onset_parent` | any parent's onset midpoint < *lt* |
| `vertical_transmission` | a grandparent **and** that grandparent's own child |
| `premature_cad` | father < 55 or mother < 65 |

Rules live in the `risk_rules` table as data; only the predicates are code. That is what
lets a clinician change thresholds without a deploy.

### Key invariants

- **Reads never compute.** Only `recompute_insights` creates artifacts, and it runs only
  after a pedigree write or in the nightly sweep. `GET /insights` serves stored rows.
- **Stable identity.** `content_hash` covers `(facts_used, fired_rules, tier,
  template:version, body)`. Identical inputs produce no new row. A committed golden
  snapshot (`tests/golden/artifacts.json`) locks this.
- **Template contract.** A template without `{not_a_diagnosis}` and `{next_step}` raises
  `TemplateContractError` at render time — a missing safety section is a crash, not a
  degraded string.
- **Sensitive rules are held.** `sensitive → status="held_for_review"`, and the read
  endpoint filters to `status="active"` only.
- **Retraction.** A condition that stops producing an outcome has its live artifact
  superseded with no replacement.

---

## 5. Half two — the chat pipeline

The **order is the safety design**. Read it top to bottom; every arrow that exits early
is an answer produced without an LLM.

```mermaid
flowchart TD
    IN["POST /api/v1/chat"] --> SAN["_sanitize_message<br/>strip C0/C1 control chars"]
    SAN --> PIV["pivot_inbound<br/>detect → translate to English"]
    PIV --> PERSIST["ensure_session + add_message(user)"]
    PERSIST --> TRIAGE["triage() — SEVERITY FLOOR<br/>none / high / emergency"]

    TRIAGE --> SCOPE{"off-topic?<br/>(only if no triage match)"}
    SCOPE -->|yes| OUT1["SCOPE_DECLINE"]
    SCOPE -->|no| ROUTE["route() → intent"]

    ROUTE --> EM{"EMERGENCY?"}
    EM -->|yes| OUT2["EMERGENCY_DIRECTIVE<br/>or SELF_HARM_REPLY<br/>NO LLM, EVER"]

    EM -->|no| CONV{"CONVERSATIONAL?"}
    CONV -->|yes| OUT3["GREETING_REPLY<br/>or IDENTITY_REPLY"]

    CONV -->|no| ABIL{"risk == NONE?"}
    ABIL -->|yes| CHAIN["11 ability handlers in a SAVEPOINT<br/>value_check → tracker → ai_result →<br/>section_detail → document → family →<br/>consult → metric → report_param →<br/>summary → suggestion"]
    CHAIN -->|hit| VAL1["validate_reply"] --> OUT4["deterministic answer<br/>+ documents / visual / citations"]

    CHAIN -->|miss| DQ{"DATA_QUERY?"}
    DQ -->|yes| OUT5["stored insights"]
    DQ -->|no| DRUG["drug interaction → drug lookup<br/>over drug_reference"]
    DRUG -->|hit| OUT6["deterministic drug reply<br/>+ MEDICATION_NOTE"]

    DRUG -->|miss| RAG["build [P] context + health snapshot<br/>+ long-term recall"]
    RAG --> SCOPEC["resolve_scope + follow-up carry-forward"]
    SCOPEC --> RET["retrieve_chunks<br/>hybrid or keyword"]
    RET --> MODE{"LLM_PROVIDER == fake<br/>and chunks?"}
    MODE -->|yes| EXTR["extractive answer<br/>verbatim validated content"]
    MODE -->|no| GEN["provider.generate<br/>ONE call"]
    GEN --> GR["analyze_grounding<br/>off / log / enforce(+1 retry)"]
    GR --> VAL2["validate_reply<br/>+ 512 registry names"]
    VAL2 -->|fail| SAFE["safe_reply(risk)"]
    VAL2 -->|pass| OUT7["grounded answer + citations"]

    OUT1 & OUT2 & OUT3 & OUT4 & OUT5 & OUT6 & EXTR & SAFE & OUT7 --> RCP["_write_receipt<br/>SHA-256 of message only"]
    RCP --> PIVO["pivot_outbound<br/>translate back, digit-checked"]
    PIVO --> STORE["add_message(assistant) + maybe_compact"]
    STORE --> RESP["ChatResult"]
```

### The response contract

```jsonc
{
  "response_message": "…",
  "risk_level": "none | high | emergency",
  "recommended_action": "call_emergency_services | seek_care_promptly | …",
  "provenance": { "path": "symptom_rag", "conditions": [...], "chunks": [...] },
  "grounding":  { "status": "grounded", "violations": [], "cited": ["1","P"] },
  "citations":  [ { "marker": "1", "source": "mcp_master_profile", … } ],
  "trace":      [ { "step": "Safety triage", "detail": "no red flags detected" }, … ],
  "visual":     { "chart_spec": {…}, "svg": "<svg …>" },
  "documents":  [ { "kind", "resource_type", "id", "slug", "title", "date", "owner" } ],
  "language":   "te-Latn",
  "session_id": "uuid"
}
```

`trace` is a **truthful decision trace** — the pipeline's actual steps, not simulated
chain-of-thought. Clients render it as the "thinking" chain.

### Safety invariants

| Invariant | Where enforced |
|---|---|
| Triage is a floor; downstream may raise, never lower | `red_flags.max_level`, ordering in `_dispatch` |
| Emergencies are answered deterministically | `orchestrator.py:258` — returns before any LLM |
| No "you have X", no numeric disease probability, no med-causation | `validation.find_banned` |
| Medication-touching replies carry `MEDICATION_NOTE` | `insights/constants.py`, prompt rules |
| HIGH/EMERGENCY replies must carry an escalation directive | `validation.has_escalation` |
| Provider/model identity is never disclosed | router → canned reply; prompt rule; `_PROVIDER_LEAK_RE` |
| Drug answers never come from the LLM | `drugs/service.py`, gated at `risk == NONE` |
| One vocabulary — compaction uses the SAME triage tables | `chat/memory.extract_flags` |
| No PHI in logs or receipts | `_write_receipt` stores `sha256(message)` |
| Everything fails open | 6 `try/except` + `logger.warning` blocks in `_dispatch` |

---

## 6. Retrieval

```mermaid
flowchart LR
    M["message"] --> RS["resolve_scope"]
    UC["user's pedigree codes"] --> RS
    REG["condition_registry<br/>511 conditions + aliases"] --> RS
    RS --> CODES["condition codes"]

    CODES --> H{"embeddings configured<br/>AND postgres?"}
    H -->|yes| VEC["pgvector cosine ANN<br/>24 candidates"]
    H -->|yes| BM["BM25 over scoped pool<br/>24 candidates"]
    VEC --> RRF["RRF fuse<br/>k=10 semantic / k=60 lexical"]
    BM --> RRF
    RRF --> SEC["section-intent boost"]
    SEC --> MMR["MMR diversity rerank"]
    MMR --> K["top k=4"]

    H -->|no| KW["token-overlap keyword rank<br/>+ section boost"]
    KW --> K
    CODES -->|empty scope| GLOB["ILIKE prefilter<br/>200 candidates"]
    GLOB --> KW
```

The asymmetric RRF constants are deliberate: a cosine rank-1 over the whole corpus is a
far stronger signal than a BM25 rank-1 over a crude token-prefiltered pool. The registry
keyword index is heavily guarded (word boundaries, ≥4-char minimum, a stricter ≥6 for
single-word aliases, an `AMBIGUITY_LIMIT` of 3, a stoplist of everyday words, and
case-sensitive matching for 3-char abbreviations so "ARM" ≠ "arm").

---

## 7. Memory model

| Horizon | Store | Content | Bound |
|---|---|---|---|
| **Within-turn** | `_dispatch` locals | patient `[P]` block, health snapshot, retrieved chunks | per request |
| **Short-term** | `conversation_messages` | last **8** turns verbatim | `KEEP_VERBATIM = 8` |
| **Mid-term** | `conversation_summaries` | deterministic structured dict — `flags`, `medications`, `boundaries`, `timeline` (sticky, never truncated); `topics`, `open_questions` (capped at 12) | compaction fires past **20** uncompacted messages |
| **Long-term** | `user_memories` | condition topics + red-flag terms, with `mention_count` / `last_seen_at`. **No raw text — no PHI.** | recall capped at 8 |

Compaction is regex/table-driven, not model-driven, so it is reproducible and cannot
hallucinate a fact into a summary. The prompt explicitly frames the compacted block as
"topics *discussed*, NOT the reader's medical record".

---

## 8. Data ownership

```mermaid
graph LR
    subgraph flyway["mhn-spring Flyway — owns ALL production schema"]
        U["user"]
        FC["family_connect · relations<br/>file_access_exclusions"]
        DOCS["reports · scans_imaging<br/>prescriptions · vaccinations<br/>unclassified_files · bills · insurance"]
        VIT["vital_reading · body_measurement<br/>lifestyle_log · manual_tracking<br/>medicine_tracking"]
        THP["traditional_health_parameters<br/>thp_age_range"]
        AI["ai_* (mhn-ai, since V4)"]
    end

    subgraph davi["Davi-owned (V6__davi_ai_tables.sql)"]
        OWN["consent_ledger · pedigree_members<br/>pedigree_conditions · risk_rules<br/>insight_templates · insight_artifacts<br/>conversation_* · user_memories<br/>mcp_chunks · condition_registry<br/>drug_reference · rag_turn_receipts<br/>symptom_logs · job_runs"]
    end

    davi -.->|"READ only"| flyway
    davi ==>|"WRITE — the only one"| VIT
```

**Rules that must not be broken.**

1. Our tables use plain `uuid` `user_id` with **no FK** to `"user"` — matching the
   `ai_processing_runs` precedent. FKs among our *own* tables are fine.
2. **Flyway owns production schema.** `db/flyway/V6__davi_ai_tables.sql` is the DDL for
   adoption into mhn-spring's chain. Our Alembic chain (version table
   `davi_alembic_version`) builds local and test databases only.
3. `"user"` and every `coredata` table are in `EXTERNAL_TABLES` and excluded from
   migrations via `include_object`.
4. `consent_ledger` is **append-only** — revoke `UPDATE`/`DELETE` from the app DB role
   in production. There is no code path that mutates it.
5. mhn-ai's output is **read, never written**: lab values from
   `content.ai.extraction.results[]` (`value_numeric` preferred, `abnormal_flag`
   authoritative), listing titles from `content.ai.classification.title`.

### Family document consent — all four conditions

Verified against Spring's `FileServiceImpl.assertCanRead`:

1. an **accepted** `family_connect` row, **and**
2. the **owner-side** read grant — `req_read` when the owner sent the request,
   `acc_read` when they accepted (legacy `*_file_share` as the NULL fallback), **and**
3. the file is not `private`, **and**
4. no `file_access_exclusions` row for `(viewer, resource_type, resource_id)`.

---

## 9. Multilingual design

There are **no per-language reply templates anywhere**. Every reply — the deterministic
safety directives included — is composed in English and translated by the sidecar.

```mermaid
sequenceDiagram
    participant U as User
    participant D as Davi
    participant T as translator sidecar
    participant P as Pipeline (English only)

    U->>D: "నాకు ఛాతీ నొప్పి ఉంది"
    D->>D: detect_language() — Unicode ranges, local & exact
    D->>T: POST /translate to_english
    T-->>D: "I have chest pain"
    D->>P: full pipeline runs on ENGLISH
    P-->>D: English reply
    D->>T: POST /translate from_english
    T-->>D: Telugu reply
    D->>D: digits_preserved()? length sane?
    D-->>U: Telugu reply (or English, fail-open)
```

The **digit-fidelity check** is the safety mechanism: if any digit sequence changes
during translation, the whole translation is discarded and the English reply is shown.
Dosages, lab values, and the Tele-MANAS helpline number (14416) cannot be corrupted by
the MT model. IndicTrans2 is a pure MT model — it never refuses, unlike a safety-tuned
LLM translator.

Latin-script detection is entirely the sidecar's job (IndicLID). Without a sidecar,
Latin-script text is treated as English and the LLM gets a reply-language directive
instead. The directive always follows the **latest** message, never the history.

---

## 10. Deployment

| Service | Config | Purpose |
|---|---|---|
| `davi-api` | `Dockerfile` + `railway.toml`, uvicorn on `$PORT`, healthcheck `/health` | the API |
| `davi-embeddings` | `railway.embeddings.toml`, `PORT=11434`, private-only, no volume | Ollama with `qwen3-embedding:0.6b` baked in |
| `translator` | `translator/Dockerfile` | IndicTrans2 + IndicXlit + IndicLID, CPU |
| nightly sweep | same image, `python -m scripts.nightly_sweep`, cron `15 3 * * *` | full recompute + 30-day purge of soft-deletes |

**No `preDeployCommand`.** On the shared database, schema has one owner. Ordering on a
shared environment: Spring migrates first, then Davi only reads and writes what exists.

**Invariant:** ingest-time and query-time embedding models must be identical. Changing
the model means re-ingesting all 16,637 chunks.

---

## 11. Testing and gates

| Gate | Command | Notes |
|---|---|---|
| Unit suite | `pytest` | aiosqlite in-memory, `-m "not pg"` by default, 33 test modules |
| Coverage | `pytest --cov=app` | `fail_under = 80`, currently ~91% |
| Postgres subset | `TEST_ALEMBIC_URL=… pytest -m pg` | reversibility + coexistence on real PG with pgvector |
| Safety evals | `python -m scripts.run_evals` | 15 scenarios through the full orchestrator |
| Golden snapshot | `tests/test_engine_api.py::test_golden_artifacts_snapshot` | regenerate with `GOLDEN_UPDATE=1` |
| Lint / types | `ruff check . && pyright` | |

Nothing in the test suite needs a live LLM, network, or GPU.

---

## 12. Configuration surface

| Variable | Default | Effect |
|---|---|---|
| `LLM_PROVIDER` | `fake` | `fake` serves **extractive** answers from the corpus |
| `GROUNDING_MODE` | `log` (code) / `enforce` (`.env.example`) | `log` ships violations to the user with a WARNING |
| `EMBEDDING_BASE_URL` | *(empty)* | unset ⇒ keyword-only retrieval |
| `TRANSLATE_BASE_URL` | *(empty)* | unset ⇒ no pivot; LLM directive only |
| `MHN_AI_BASE_URL` | *(empty)* | unset ⇒ chat uploads stay unprocessed (retryable) |
| `AUTH_ENABLED` | `false` | `false` means `X-User-Id` header identity — **never on a shared DB** |

Note the defaults: **out of the box this app answers from a canned/extractive path with
keyword retrieval, no translation, and no auth.** Each production capability is an
opt-in env var.

---

## 13. Reading order for a new engineer

1. `CLAUDE.md` + `README.md` — the invariants and the why.
2. `app/chat/orchestrator.py:200-500` — `_dispatch`. This is the product.
3. `app/triage/red_flags.py` — the floor, and the one vocabulary.
4. `app/insights/core.py` — the pure engine, top to bottom, in one sitting.
5. `docs/production_integration.md` — every contract with the other three repos.
6. `evals/scenarios.json` — what "safe" means, executably.

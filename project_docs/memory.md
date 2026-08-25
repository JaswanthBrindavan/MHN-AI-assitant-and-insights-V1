# Project Memory

Durable context for anyone — human or agent — picking this codebase up. Facts
that are **not** obvious from the code, and that cost real time to discover.

For *what was built*, read [`implementation-log.md`](./implementation-log.md).
For *what to do next*, read [`handover.md`](./handover.md).

---

## What this system is, in one paragraph

A healthtech backend for Indian families with two halves that share a database
and auth: a **deterministic insights engine** (family history → reproducible,
hash-identified insight artifacts, no LLM anywhere) and a **chat chassis**
(triage floor → tools/RAG → LLM → grounding → validation → receipts). It is
decision support, never diagnosis, and that is enforced in code — a banned-phrase
validator, mandatory template sections, and emergency-before-LLM ordering — not
in policy documents.

---

## The invariants. Break these and the product is a different, worse thing.

1. **The triage floor runs before everything.** Before any handler, any tool,
   any model. It is a severity FLOOR: downstream may raise a level, never lower
   it. Emergencies are answered deterministically and the model is never asked —
   tests assert `provider.calls == []`, not merely that the model was overruled.
2. **Reads never compute.** Only `recompute_insights` creates artifacts, after a
   pedigree write or in the nightly sweep. `GET /insights` serves stored rows.
3. **Purity.** `insights/core.py`, `grounding/claims.py`, `chat/memory.py`,
   `grounding/fidelity.py`, `llm/tools.py` are stdlib-only and side-effect free.
4. **Fail open, always.** Grounding, validation, receipts, compaction, profile,
   episodes, tools — every one degrades to a safe reply plus a WARNING. A
   guardrail must never become a new way to break a reply.
5. **No PHI in logs or receipts.** Receipts store `sha256(message)`. Tool
   arguments are never logged, because they carry patient data.
6. **The provider is never disclosed.** Identity questions route to a canned
   reply; the validator bans provider names in generated text; the trace names
   no model.
7. **Flyway owns production schema.** Our Alembic chain builds local and test
   databases only. New tables ship as `db/flyway/V*__davi_*.sql` for adoption
   into mhn-spring.
8. **`consent_ledger` is append-only.** A revocation is a new row. The record
   that consent existed outlives the data it permitted.

---

## Things that are true and surprising

**`utcnow()` is deliberately monotonic.** The system clock is coarser than a
burst of inserts — 1000 consecutive `datetime.now()` calls returned ONE distinct
value on the dev machine. `conversation_messages` is ordered by
`(created_at, id)` in six places and `id` is a random uuid4, so ties made message
order random. That silently reordered what the model sees as recent turns and
made compaction fold the wrong messages. Do not "simplify" this back.

**Tool calls execute SEQUENTIALLY, on purpose.** Every executor shares one
`AsyncSession` and SQLAlchemy refuses concurrent operations on one. Measured with
`asyncio.gather`: the first of four calls succeeded and the rest returned "could
not be completed" on perfectly good data. The parallelism was never real — one
session is one connection. A regression test fails if anyone reintroduces
`gather`.

**`FakeProvider.DEFAULT` is load-bearing and must not be reworded.** It
deliberately contains no "clinician". `evals/scenarios.json` proves the
provider-outage path by asserting the reply *contains* "clinician" — a word only
the real degraded safe reply supplies. Change the text and that scenario passes
whether or not the provider ever failed.

**SQLite returns naive datetimes** for a `DateTime(timezone=True)` column;
PostgreSQL returns aware ones. Comparing them raises. See `episodes._aware`.

**`_hybrid_rank` returns `None` on any non-PostgreSQL dialect**, so the entire
hybrid retrieval path is skipped under the default SQLite test suite. It has
never run in CI until the Task 28 workflow.

**Voice transcription runs BEFORE the triage floor, and the floor runs even
when the transcript is not trusted.** Returning the low-confidence confirmation
with `risk_level=NONE` was a real breach — that is *lowering* the floor, not
skipping it, and ASR confidence collapses on exactly the breathless, panicked
speech that signals an emergency.

**Vision output is not a numeric source.** `ToolResult.trusted_values` is False
for `analyze_image`, so an OCR misread cannot be authorised by the
numeric-fidelity guard. Anything a MODEL produced belongs on the same footing.

**`ensure_session` checks ownership.** It did not until the Phase 3 review;
passing another user's `session_id` loaded their history into your prompt.

**Windows cp1252 vs UTF-8.** `Path.read_text()` without `encoding=` crashes on
the em-dashes in `scenarios.json`. This made the safety eval gate completely
unrunnable on Windows for some time. Always pass `encoding="utf-8"`.

---

## The two chat engines

`CHAT_ENGINE=legacy` (default) | `agentic`.

- **legacy** — eleven regex-gated handlers tried in order; first match wins.
- **agentic** — the same abilities offered to the model as tools.

Both pass `scripts/run_evals` 15/15. Both must keep passing. The legacy engine
is the fallback that currently answers real users, and Task 12 (deleting it) is
gated on a week of staging that has not happened.

`scripts/run_evals` is engine-aware: a scenario pins BEHAVIOUR, not which engine
produced it, and can script tool calls, because a deterministic fake cannot
*decide* to call one.

---

## Layout of what was added

```
app/llm/tools.py          provider-neutral tool vocabulary (pure)
app/llm/anthropic.py      Anthropic adapter (official SDK)
app/llm/openai_compat.py  OpenAI-compatible adapter (httpx) — the self-host path
app/chat/agent.py         bounded loop + one-shot recovery
app/chat/tools/           definitions · executors · registry (fail-closed)
app/chat/streaming.py     per-sentence validation for SSE
app/chat/profile.py       consent-gated user profile
app/chat/episodes.py      symptom episodes
app/chat/summarize.py     prose alongside deterministic compaction
app/grounding/fidelity.py numeric fidelity guards (pure)
app/api/v1/profile.py     see / change / erase what is remembered
app/documents/fetch.py    Spring-minted presigned GET, two-sided consent
app/vision/service.py     read an image; output is untrusted text
app/voice/service.py      self-hosted ASR/TTS sidecar
scripts/provider_bakeoff.py  Anthropic vs self-hosted, with numbers
.github/workflows/ci.yml  the gate that did not exist
```

---

## Working agreements learned the hard way

**Reviewers must be read-only.** Review agents given write access left scratch
tests that monkeypatched global state without cleanup; with them present the
emergency-ordering tests failed — pure artifact, and the most alarming possible
false signal. One left a `# MUTANT` line in production code.

**Read the verification output, do not just produce it.** One commit went in
with ruff and pyright errors visible in the same terminal output.

**Verify a claim before repeating it.** The plan asserted things about the repo
that were not true. The pre-coding audit each task starts with exists for that
reason and has paid for itself every time.

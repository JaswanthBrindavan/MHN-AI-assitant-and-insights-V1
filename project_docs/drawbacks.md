# Davi Health AI — Drawbacks & Gap Analysis

**Target:** perform like an **August.ai**-class conversational health assistant.
**Baseline:** this repo at commit `fee14d4` (branch `praveen-mhn`).
**Companion:** [`architecture.md`](./architecture.md).

---

## 0. How to read this document

Every drawback below is **evidence-backed** — it cites the file and line in this
codebase that produces it. Nothing here is speculative about our own code.

The August.ai side is different. What I am comparing against is that product's
**public positioning and observable behaviour** as a WhatsApp-first, India-focused AI
health companion: warm conversational tone, follow-up questioning like a doctor,
memory of the user across time, photo/report understanding, voice, multiple Indian
languages, and proactive check-ins. I have **no visibility into their internals**, and
nothing below should be read as a claim about their implementation. Where I say
"August-class", read it as "a product with those behaviours".

**Severity key:** 🔴 blocks the goal · 🟠 materially degrades it · 🟡 friction/debt.

---

## 1. Executive summary

> This codebase is an outstanding **clinical safety and compliance chassis** and a
> weak **conversational product**. Those are not the same thing, and the architecture
> optimises hard for the first at the direct expense of the second.

The design thesis — *"the LLM is the last component, and every deterministic gate that
can answer without it, should"* — is exactly right for insurance, audit, and regulatory
defensibility. It is exactly wrong for the experience August.ai sells, which is a model
that *feels like a knowledgeable person* holding a conversation.

Concretely, the single most consequential structural fact:

```
app/chat/orchestrator.py — the LLM is reached at line 650 of 854,
after 11 regex-gated handlers, a drug lookup, and 5 early-return paths.
It is called EXACTLY ONCE, with no tools, no ability to ask a
follow-up question, and no ability to fetch data it decides it needs.
```

The LLM in this system is a **leaf node**. In an August-class product it is the
**root** — the thing that decides what to do, what to ask, and what to look up. Nearly
every drawback below is a downstream consequence of that one inversion.

**Six things gate the goal.** Everything else is secondary:

| # | Gap | Severity |
|---|---|---|
| 1 | No tool/function calling — LLM cannot reach the user's own data | 🔴 |
| 2 | No clarifying-question loop — answers immediately, never asks | 🔴 |
| 3 | No streaming — every reply lands as a wall of text after 3–12s | 🔴 |
| 4 | Regex intent routing — an unbounded maintenance treadmill | 🔴 |
| 5 | No image/voice understanding | 🔴 |
| 6 | Shallow memory — 8 turns + a keyword dict; no user model | 🟠 |

---

## 2. The structural drawback

### 2.1 🔴 The LLM cannot reach the user's own data

Data abilities (documents, labs, vitals, trackers, family records) are reachable
**only** through deterministic regex parsers that run *before* the model
(`orchestrator.py:303-357`). If a parser does not match, that data never enters the
prompt. If a parser *does* match, the pipeline returns immediately and the LLM is never
called at all.

This means composite questions are structurally unanswerable:

> *"My last report showed creatinine 1.4 — given that my father has kidney disease,
> should I be worried?"*

- `handle_report_param_ask` may match "creatinine" → returns the number, **no reasoning**.
- Or nothing matches → falls to RAG → the model reasons about kidney disease **without
  ever seeing the user's 1.4**.

There is no path in which both facts reach the same reasoning step. The `[P]` block
(`chat/context.py`) is a partial mitigation, but it is a *fixed* snapshot injected only
when `is_personal_health_query()` heuristically fires — not something the model can
query.

**Fix direction:** convert the 11 handlers into **tools** the model calls. They are
already clean, side-effect-scoped async functions returning dicts — the refactor is
mostly mechanical. This is the highest-leverage change in the document.

### 2.2 🔴 The assistant never asks a question

A doctor's first move is to *ask*: how long, how severe, any fever, what medications,
has this happened before. This pipeline's first move is always to *answer*.

There is no clarification state, no slot-filling, no symptom-triage dialogue. The only
multi-turn machinery is the fasting/post-meal glucose clarification
(`data_handlers.py:188-251`) — one hardcoded special case, which proves the general
mechanism is absent.

The prompt tells the model recent turns are "likely a follow-up" (`rag/prompt.py:86-94`)
but nothing invites the model to *initiate* one, and the single-call architecture gives
it nowhere to put the answer.

**This is the biggest experiential gap.** It is what makes a health assistant feel like
a doctor rather than a search engine, and it is entirely absent.

### 2.3 🔴 Single-shot generation, no agentic loop

One `provider.generate()` call (`orchestrator.py:650`). The only second call is the
grounding-corrective retry in `enforce` mode (`orchestrator.py:184`). There is no
re-retrieval when the first retrieval is poor, no decomposition of a multi-part
question, no self-check, no planning.

---

## 3. Conversational capability

### 3.1 🔴 No streaming

`LLMProvider.generate()` returns `str` (`llm/base.py`). `OpenAICompatibleProvider` sends
`"stream": False` explicitly (`llm/providers.py:86`). The production integration doc even
concedes the workaround: *"the client-side typewriter supplies the streaming feel."*

A fake typewriter over an already-complete response does not fix time-to-first-token. On
a non-English turn the user waits for: sidecar detect → sidecar translate-in → 11
sequential DB handler probes → retrieval → **full LLM generation** → grounding → validation
→ sidecar translate-out. Realistically **3–12 seconds of silence**.

August-class products stream. This one cannot without changing the provider protocol,
the grounding step (which needs the whole answer), and the validator (same). Streaming is
therefore not a small change — but it is a required one.

### 3.2 🟠 Failure mode is a dead end, not a recovery

When the validator rejects a reply, the entire answer is discarded and replaced with one
fixed sentence (`orchestrator.py:705`, `replies.py:54`):

> *"I want to be careful here, so I'll keep this general… best to speak with a clinician."*

The user gets a non-answer with **no explanation of what happened and no path forward**.
There is no retry with a corrected instruction (outside `enforce` grounding), no
narrower answer, no "let me put that differently". Two rejections in a row and the bot
appears broken.

### 3.3 🟠 Canned replies are literally identical every time

`GREETING_REPLY`, `IDENTITY_REPLY`, `SCOPE_DECLINE`, `HIGH_ESCALATION`,
`_SAFE_NONE`, `EMERGENCY_DIRECTIVE` — one hardcoded string each (`chat/replies.py`).
Every greeting in every session for every user is the same 3 sentences. This reads as
robotic within two exchanges, and it is the *first* thing a user encounters.

### 3.4 🟠 One voice for everyone

No tone adaptation, no reading-level adaptation, no persona. A 22-year-old asking about
acne and a 70-year-old asking about heart failure get identically-registered prose.

### 3.5 🟡 The emergency path terminates the conversation

`orchestrator.py:258-284` returns a single fixed directive and stops. No "is the person
conscious?", no "here's what to do while you wait", no follow-through. Clinically
defensible; conversationally, the moment the user needs the most support is the moment
the product goes silent.

### 3.6 🟡 The trace leaks clinical jargon to patients

`trace` exposes matched red-flag phrases verbatim, MC condition codes, and chunk counts
(`orchestrator.py:120-130`). Great for a demo and for debugging; for a patient,
`"matched: 'blood in stool'"` rendered in a thinking chain is alarming rather than
reassuring.

---

## 4. Architecture & extensibility

### 4.1 🔴 The regex treadmill

`chat/abilities.py` is 737 lines of hand-written regex parsers. `chat/data_handlers.py`
is 1,380 lines of handlers. Every new phrasing a real user invents requires a code
change, a test, a review, and a deploy.

**The git log is the evidence.** These are all the same bug reported in different words:

```
9e49e19  fix(family): grandchild/extended relations + name-based document asks
e1f36b9  fix(params): bare "latest X" phrasing; "normal" flags never read as deviations
dabe0ce  fix(metrics): match real-world extraction names; guard against A1c/HDL traps
2a3b69b  fix(docs): "list / view / display / all / do I have" fetch documents
fbb5b3c  fix(docs): generic document words fetch everything; pending uploads listed
```

Five of the last fifteen commits are "the parser didn't understand this phrasing."
This curve does not flatten — natural language phrasings are unbounded. An LLM-based
intent/tool layer collapses this whole class of work.

### 4.2 🔴 `_dispatch` is an 800-line function with fragile ordering

Eleven handlers tried sequentially (`orchestrator.py:313-351`), and the ordering carries
load-bearing correctness that is documented only in comments:

```python
# AI-result requests outrank the document LISTING — "get insights for
# this report" must fetch the pipeline's result, not list files.
# Detail questions about a section ("policy number", "bill amount")
# outrank the LISTING of that section.
```

Adding a twelfth handler means re-reasoning about its position against eleven others.
There is no precedence model, no confidence score, no way to detect that two parsers
both matched — first match wins, silently.

### 4.3 🟠 Six nested fail-opens hide real bugs

`_dispatch` contains six `try/except Exception → logger.warning` blocks. The design
intent (a guardrail must never break a reply) is correct. The consequence is that a
genuine bug in a handler produces a *plausible generic answer* and a WARNING nobody
reads. With no metrics or alerting (§8.1), degradation is invisible.

### 4.4 🟠 Everything runs sequentially

Within one turn, these are all independent yet all `await`ed in series
(`orchestrator.py:487-560`): `build_patient_context` → `build_health_snapshot` →
`assemble_context` → `resolve_scope` ×2 → `retrieve_chunks` → `load_condition_index` →
`record_topics` → `recall`. That is 8+ sequential round trips before generation starts.
`asyncio.gather` would remove most of that latency for free.

### 4.5 🟡 Patient context is recomputed every single turn

`build_patient_context` re-queries `pedigree_conditions` and `insight_artifacts` on every
message (`chat/context.py:33`). It changes only on a pedigree write. No caching.

### 4.6 🟡 Process-global registry cache with no invalidation

`knowledge/registry.py:_index_cache` is a module global, reset only by an explicit
`reset_index_cache()` call. After an ingest, running API processes serve a stale index
until restarted.

---

## 5. Knowledge & retrieval

### 5.1 🔴 Closed corpus — 511 conditions and nothing else

Outside the Master Condition Profiles, the model answers from `[GK]` general knowledge
with no grounding, no citation, and no verification. There is no web access, no
guidelines feed, no update path short of re-ingesting docx files.

### 5.2 🔴 No drug interaction data at all

Stated plainly in `drugs/service.py:255`: *"The drug_reference dataset carries no
interaction data."* The interaction handler exists purely to **refuse gracefully** and
route to a pharmacist. For a market where polypharmacy and self-medication are common,
"can I take X with Y" is a top-tier question that the product cannot answer.

### 5.3 🟠 Keyword retrieval is the *default*

`EMBEDDING_BASE_URL` is empty by default (`config.py:46`). Without it, retrieval is
token-overlap counting (`retrieval.py:_keyword_rank`) — poor for the colloquial,
code-mixed phrasing real Indian users type. Enabling hybrid search requires a second
Railway service *and* a one-time GPU ingest of 16,637 chunks.

### 5.4 🟠 The registry index is a hand-tuned brittleness surface

`_ALIAS_STOPLIST` (≈90 hand-listed words), `_GENERIC_HEAD_WORDS` (≈60), `MIN_KEYWORD_LEN`,
`MIN_SINGLE_WORD_ALIAS_LEN`, `AMBIGUITY_LIMIT`. The comments document real production
hijacks that had to be patched by hand:

- *"'hindi' appeared as an alias of 22 conditions"*
- *"'I missed my dose' must never scope to Miscarriage"*
- *"'ARM' (age-related maculopathy) must not fire on the word 'arm'"*

Every one of these is a symptom of lexical matching doing a semantic job. Each fix is
local; the class of bug is unbounded.

### 5.5 🟠 Unscoped symptom questions get an `ILIKE` scan

When no condition is named, retrieval falls back to `ILIKE '%token%'` over up to 200
candidate rows (`retrieval.py:_global_fallback_rows`). No index, full scan of
`mcp_chunks`, and purely lexical relevance — precisely the case (symptom description
with no diagnosis named) that matters most for a first-time user.

### 5.6 🟡 Grounding only checks sentences containing numbers

`is_factual()` returns true only for unit or threshold patterns
(`grounding/claims.py:_UNIT_RE`, `_THRESHOLD_RE`). A confidently wrong sentence with no
digits — *"that symptom usually resolves on its own"* — is not "factual" by this
definition and is **never grounding-checked**. It is caught only if it happens to trip
the fixed banned-phrase list.

### 5.7 🟡 `GROUNDING_MODE` defaults to `log`

`config.py:51` defaults to `log` (`.env.example` says `enforce`; the code default wins if
the var is unset). In `log` mode, ungrounded clinical claims are **shipped to the user**
and merely logged. Meanwhile `enforce` doubles LLM cost and latency on every violation.

---

## 6. Multimodal & channel

### 6.1 🔴 No image understanding

*"Davi does NOTHING with the document itself"* (`documents/service.py:3`) — no bytes, no
AWS credentials, by deliberate design. Davi reads only the `content.ai` JSON that mhn-ai
already extracted.

So the following all fail:

- a photo of a skin rash, a swollen joint, an eye
- a photo of a pill strip ("what is this tablet?")
- a handwritten prescription
- any report mhn-ai's classifier did not handle
- **any question about a document's actual layout, image, or free text**

For a WhatsApp-native audience, sending a photo is the *default* interaction. This is a
hard architectural boundary, not a missing feature — and one worth revisiting, since
the security rationale ("no AWS creds") can be satisfied with a scoped read-only
presigned-GET path rather than by abstaining entirely.

### 6.2 🔴 No voice

No ASR, no TTS, anywhere in the repo. Voice notes are how a very large share of Indian
WhatsApp users communicate, especially the older and lower-literacy segments that
benefit most from a health assistant.

### 6.3 🔴 No messaging channel

HTTP JSON only, plus a dev HTML console. No WhatsApp Business API, no webhook handler,
no message-template management, no delivery/read receipts, no session-window handling.
If WhatsApp is the distribution channel, that is a whole subsystem that does not exist.

### 6.4 🟠 Translation is machine-flat and adds two round trips

Every non-English turn costs **two extra sidecar calls** (detect + translate-in, then
translate-out) against CPU-hosted IndicTrans2, serialized around the LLM call.

Worse for quality: the deterministic safety replies are written in English and
machine-translated. `EMERGENCY_DIRECTIVE` and `SELF_HARM_REPLY` — the two most
emotionally-loaded strings in the product — reach a Telugu or Bengali user as MT output.
The digit-fidelity guard protects the *numbers*; nothing protects the *warmth*.

### 6.5 🟡 Romanized Indic is invisible without the sidecar

`i18n/language.py` is Unicode-script only. "mujhe bahut tez sar dard ho raha hai" is
detected as English unless `TRANSLATE_BASE_URL` is configured. Romanized typing is the
majority input mode for Hindi on phones.

---

## 7. Memory & personalization

### 7.1 🟠 Eight turns of real memory

`KEEP_VERBATIM = 8` (`chat/conversation.py:22`). Beyond that, history collapses into a
regex-extracted dict of `flags / medications / boundaries / timeline / topics /
open_questions`, capped at 12 items for the capped keys.

The reproducibility argument for deterministic compaction is genuinely strong. The cost
is that **nuance is destroyed**: "my mother died of this last year and I'm frightened"
compacts to a topic code. Nothing about the user's emotional state, their situation, or
what has already been explained to them survives.

### 7.2 🟠 No user model

`user_memories` stores condition topics and red-flag terms only — deliberately, to avoid
PHI (`chat/long_term.py:5`). There is nowhere to hold: age, sex, pregnancy status,
current medications, allergies, chronic conditions, prior advice given, stated goals,
preferred language, communication preferences, or open episodes.

An August-class assistant's entire value proposition is *"it knows me."* This one knows
what you asked about.

### 7.3 🟠 No episode tracking

If a user reports fever on Monday, there is no representation of "an active fever
episode" — no resolution tracking, no worsening detection, no follow-up. Thursday's
"still not better" is treated as a fresh question.
`ActiveSymptomState` exists as a model but **no code path writes to it.**

### 7.4 🔴 No proactive messaging

Nothing outbound except the nightly recompute cron. No check-ins ("how's the fever?"),
no medication reminders, no report-ready notifications, no re-engagement. The product is
purely reactive: it exists only in the instant a user types.

Proactive follow-up is a defining behaviour of the target product and a **complete
subsystem** here — scheduler, outbound channel, consent, quiet hours, rate limits.

### 7.5 🟡 `user_memories` grows unbounded

Rows accumulate per user with no TTL, no decay, and no user-facing view or delete. For a
health product under DPDP-style regimes, "show me what you remember / forget this" is a
requirement, not a nicety.

---

## 8. Operations, evaluation, and cost

### 8.1 🔴 No observability

No metrics, no tracing, no dashboards, no alerting. `rag_turn_receipts` is an audit
trail, not telemetry — it cannot answer *"what is p95 latency", "what fraction of replies
degraded to the safe reply last week", "which handler fires most", "how many turns hit
the provider-error path"*. Combined with six silent fail-opens (§4.3), the system can
degrade badly and look fine.

### 8.2 🔴 No feedback loop

No thumbs up/down, no correction capture, no conversation-quality review queue, no way
for a failure to become a test. Improvement is entirely developer-initiated from
anecdote.

### 8.3 🟠 15 eval scenarios

`evals/scenarios.json` covers 15 cases, all safety invariants (emergency detection,
scope decline, banned phrasing). There is **no quality eval at all** — nothing measuring
answer helpfulness, retrieval relevance, tone, or task success. Safety is measured;
usefulness is not.

The assets exist to fix this cheaply — `evals/questions_10k.csv`,
`evals/realistic_questions.json`, `scripts/question_sweep.py`, `scripts/hybrid_eval.py`
— but no graded quality suite is wired into CI.

### 8.4 🟠 Tests run on SQLite; production is Postgres

`conftest.py` builds an in-memory aiosqlite DB from `Base.metadata`. Production behaviour
that SQLite cannot exercise: pgvector similarity, PG enum binds, partial unique indexes,
`ILIKE` semantics, concurrent-transaction behaviour, and the entire hybrid retrieval path
(which `_hybrid_rank` skips outright on non-Postgres). The `pg`-marked subset covers
migrations, and CLAUDE.md notes it is run manually on a Homebrew Postgres — not in CI.

### 8.5 🟠 No prompt caching and no context budget

Every RAG turn sends the full system prompt: safety rules + grounding rules +
personalization rules + up to 6 verbatim turns + compacted JSON + 4 chunks + patient
context + recall + language directive. No provider-side prompt caching (the static
safety/grounding prefix is a perfect cache candidate), no token budgeting, no adaptive
chunk count. At scale this is the dominant cost line, and it is entirely unmanaged.

### 8.6 🟡 No rate limiting or abuse handling in Davi

Delegated to the React BFF (`withRateLimit()`). Any other caller — the SERVICE_TOKEN
path included — is unthrottled.

### 8.7 🟡 Sensitive insights are computed and then buried

`sensitive → status="held_for_review"` (`insights/engine.py:151`) and `GET /insights`
filters to `active` only. **No clinician review queue, tool, or endpoint exists.** These
artifacts are generated and never seen by anyone, forever.

### 8.8 🟡 All clinical content is DRAFT

Every phrase table, threshold, onset midpoint, certainty weight, reference range, and
template is marked *"DRAFT — pending clinician sign-off"*. This is honest and correct,
but it is a **release blocker** that no amount of engineering removes. It needs a named
clinician, a review process, and a sign-off artifact.

---

## 9. Gap table vs. an August.ai-class product

| Capability | August-class expectation | This codebase | Gap |
|---|---|---|---|
| Streaming replies | Token-by-token | Blocking JSON, fake typewriter | 🔴 |
| Asks follow-up questions | Core behaviour | Never (1 hardcoded exception) | 🔴 |
| LLM reaches user's data | Tool calling | Regex gates, LLM is a leaf | 🔴 |
| Image understanding | Reports, rashes, pill strips | None — no file bytes by design | 🔴 |
| Voice notes | Expected on WhatsApp | None | 🔴 |
| WhatsApp channel | The product | HTTP JSON + dev console | 🔴 |
| Proactive check-ins | Defining feature | None | 🔴 |
| Remembers the user | Rich longitudinal profile | Topic codes + flags only | 🟠 |
| Episode tracking | "How's the fever today?" | Model exists, never written | 🟠 |
| Tone / warmth | Human, varied, adaptive | Fixed strings, one register | 🟠 |
| Multilingual | Native-feeling | MT pivot, machine-flat | 🟠 |
| Drug interactions | Common ask | Explicitly refuses | 🟠 |
| Broad knowledge | Open-ended | 511-condition closed corpus | 🟠 |
| Quality measurement | Continuous | 15 safety scenarios, zero quality | 🟠 |
| **Clinical safety floor** | Varies | **Best-in-class** | ✅ |
| **Auditability / receipts** | Varies | **Best-in-class** | ✅ |
| **Reproducible insights** | Absent | **Unique differentiator** | ✅ |
| **Family-consent enforcement** | Absent | **Verified against Spring** | ✅ |

The three ✅ rows are real, hard-won moats. **Nothing in the recommendations below
should trade them away** — the goal is to keep the safety chassis and put a real
conversational engine inside it.

---

## 10. Recommended sequence

Ordered by *(impact on the August.ai goal) ÷ (effort)*.

### Phase 1 — unblock the conversation (highest leverage)

1. **Tool calling.** Convert the 11 ability handlers into tools the model invokes.
   They are already clean async functions returning dicts. Keep the triage floor and the
   emergency path *before* the model, untouched — the floor stays a floor. Everything
   below `risk == NONE` becomes tools.
   → kills §2.1, §4.1, §4.2 in one change.
2. **Streaming.** Add `generate_stream()` to `LLMProvider`. Run the validator on
   accumulated text and hold the last sentence back until grounding clears — stream the
   safe prefix, verify the tail.
   → kills §3.1.
3. **Clarifying questions.** Let the model ask when the question is underspecified, with
   a bounded turn budget (2 questions max) so it cannot loop.
   → kills §2.2.

### Phase 2 — make it feel like it knows you

4. **User profile store.** Age, sex, chronic conditions, medications, allergies, stated
   goals, language preference — with explicit consent, a user-facing view, and delete.
5. **Episode tracking.** Actually write `ActiveSymptomState`. Open/resolve/escalate.
6. **LLM-assisted compaction** alongside the deterministic one — keep the structured
   dict for reproducibility, add a prose summary for nuance. Deterministic stays
   authoritative for safety-relevant fields.

### Phase 3 — meet users where they are

7. **WhatsApp channel** — webhook, session windows, templates, delivery receipts.
8. **Vision** — scoped read-only presigned-GET, VLM over report/rash/pill-strip images.
   The "no AWS credentials" posture can be preserved with a narrow, audited path.
9. **Voice** — ASR in, TTS out, Indic-capable.
10. **Proactive follow-up** — scheduler + consent + quiet hours + rate limits.

### Phase 4 — know whether any of it worked

11. **Observability** — latency/degradation/handler-hit metrics, tracing, alerting on
    the fail-open paths.
12. **Quality evals** — build a graded suite on `questions_10k.csv`; measure retrieval
    relevance and answer quality in CI, not just safety.
13. **Feedback capture** — thumbs, corrections, and a path from a bad reply to a test case.
14. **Prompt caching + context budget** — cache the static safety prefix; cap and
    prioritize the dynamic tail.

### Non-negotiable throughout

- The **triage floor runs before the model**, always. Emergencies stay deterministic.
- The **validator runs on everything**, including streamed and tool-composed output.
- **Receipts, no PHI in logs, append-only consent** — unchanged.
- Get the **clinician sign-off** on the DRAFT content. It is the only item here that
  engineering cannot solve, and it gates launch regardless of how good the chat becomes.

---

## 11. The honest summary

This is a genuinely well-built system that has been optimised against a different
objective function than the one now being asked of it. The safety architecture, the
reproducible insights engine, the consent enforcement, and the fail-open discipline are
better than what most health-AI products ship — they are worth keeping intact.

But an August.ai-class experience requires the LLM to be the **orchestrator**, not the
final formatting step. The good news is that the chassis makes that inversion *safe*:
the triage floor, the validator, the grounding verifier, and the receipts all continue
to work regardless of whether the model is called once at the end or ten times in a loop.

**Keep the chassis. Replace the engine.**

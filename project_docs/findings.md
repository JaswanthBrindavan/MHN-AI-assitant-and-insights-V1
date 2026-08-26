# Review Findings

Findings from the independent review pass on each task, per
`.claude/review-rules.md`. Severity: **Critical** · **High** · **Medium** ·
**Low** · **Informational**.

Each task's findings are reviewed by adversarial agents that try to *refute*
them before they are recorded here — so a finding listed as confirmed survived
someone actively arguing it was wrong. Refuted findings are listed too, so they
do not get re-litigated later.

---

## Task 1 — Internal tool-calling vocabulary

**Review:** 17 agents — 4 lenses (requirement coverage, logic/edge cases,
integration/regressions, test quality) → per-finding adversarial verification →
consolidated verdict.

**Verdict: no Critical, no High.** Five Low findings, all confirmed and fixed.

### Pre-coding audit (before any code was written)

A separate 10-agent sweep found the plan contradicted the repository. These are
plan defects, not implementation defects.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| A1 | **High** | The plan renamed `FakeProvider.DEFAULT` → `_DEFAULT_REPLY` with text containing "clinician". `evals/scenarios.json` proves the provider-outage path by asserting `reply_contains: "clinician"` — a word supplied by the *real* degraded safe reply. The new text would have made that scenario pass whether or not the provider ever failed. | Rejected the rename. `DEFAULT` kept byte-for-byte. Proved non-vacuous: neutering the outage drops run_evals to 14/15. |
| A2 | Medium | Plan typed `raises: BaseException`. The orchestrator's fail-open catches `except Exception`, so a `BaseException` would escape it and the outage tests would stop exercising the degrade. | Typed `Exception`. |
| A3 | Medium | Plan raised *before* recording the call, so `provider.calls == []` would no longer mean "the model was never reached". | Record first, then raise. |
| A4 | Low | Plan used `Message = Union[...]`. ruff `select = ["UP"]` + `py311` → UP007 failure, and the task's own gate is `ruff check .`. | Used `X \| Y \| Z`. |
| A5 | **Plan bug** | Task 6's budget test builds 5 scripted turns but asserts on the 3rd. `assert out.text == "final"` fails under **any** implementation. | Plan amended: `range(2)`. |
| A6 | Informational | Task 7's acceptance gate (`CHAT_ENGINE=agentic run_evals` = 15/15) will fail for an unrelated reason: `_dispatch_agentic` returns `provenance["path"] == "agentic"` but two scenarios expect `"tracker_add"` / `"symptom_rag"`. | Deferred to Task 7 — make the path expectation engine-aware. |

### Implementation review

| # | Severity | File | Finding | Fix |
|---|---|---|---|---|
| L1 | Low | `tests/test_llm_tools.py` | `wants_tools`' `stop_reason` half was unverified — replacing the body with `return bool(self.tool_calls)` left all 8 tests green. This is the **sole loop condition** for Task 6's agent. | Added the two missing cases. **Mutation-checked**: the mutant now fails. |
| L2 | Low | `app/llm/fake.py` | `raises` was checked *before* the script, making `responses=` and `raises=` mutually exclusive — which is why a third outage stub had to survive, hand-copying the `calls` dict shape. Change the shape and that copy silently writes the old one. | Swapped the order so they compose. `ScriptThenRaiseProvider` deleted. Net-negative diff. |
| L3 | Low | `tests/test_llm_tools.py` | Three weak assertions: `len(msg.results) == 2` was vacuous; nothing pinned the load-bearing `list(messages)` snapshot; the frozen test mutated the shared module-level `SPEC`, so one defect would produce a second confusing failure elsewhere. | All three fixed; added a snapshot-not-alias test. |
| L4 | Low | `.gitignore` | `.claude/` listed twice; `CLAUDE.md` rule inert (the file is tracked); **`project_docs/` ignored** — so the plan driving 28 tasks could not be committed without `-f`. | All three removed. `project_docs/` is now tracked. |
| L5 | Low | `CLAUDE.md` | Task Plans section pointed at `docs/` (which holds production contracts, not plans); the `llm/` layout line did not mention `tools.py` or `ToolCallingProvider`. | Both corrected. |

### Refuted (raised in review, verified as *not* defects — do not re-litigate)

- **Frozen dataclasses with `dict` fields are a mutability hazard.** True in
  general, not here: no code mutates a `ToolSpec.input_schema` or
  `ToolCall.arguments` after construction, and deep-freezing would force
  callers to build immutable JSON-Schema structures for no benefit.
- **`stop_reason: str` should be `Literal[...]`.** Verified against this repo's
  pyright config: it cannot see through `resp.json()` → `Any`, so it would flag
  the *correct* call site and stay silent on the raw pass-through it was meant
  to catch. Revisit in Tasks 2–3 with annotated response types.
- **A 7-way parametrized frozen test.** 31 frozen dataclasses already ship in
  `app/` with zero frozen coverage; inventing a convention here is out of scope.

### Inherited defects (from `origin/main`, verified standalone)

Not introduced by this work. Confirmed by checking out `origin/main` with none
of this branch's commits present.

| Severity | File | Problem | Fix |
|---|---|---|---|
| **High** | 5 files | `Path.read_text()` / `write_text()` with no `encoding=`. Raised `UnicodeDecodeError` under Windows cp1252 on the em-dashes in `scenarios.json` — **`scripts/run_evals` and `tests/test_evals.py` could not run at all on Windows.** The safety eval gate was silently unavailable. | `encoding="utf-8"` on all six call sites. |
| Medium | `tests/test_chat_uploads_history.py:1072` | pyright: subscripting a possibly-`None` `name_check`. | Narrowed with an explicit `is not None` assert. |
| Low | `app/api/v1/admin.py:17` | ruff `I001` unsorted imports. | `ruff --fix`. |
| Low | `tests/test_admin_registry_refresh.py:89,111` | ruff `E501` ×2. | Wrapped. |

> The repo's stated gate is `ruff check . && pyright` (README). Those PRs landed
> without it. Worth a CI gate — see Task 28.

### Cross-cutting defect found during Task 1

| Severity | Problem |
|---|---|
| **High** | **Message ordering was nondeterministic.** `utcnow()` returned one distinct value across 1000 consecutive calls; rows sharing `created_at` fell back to a random uuid4 tiebreak. Six call sites order `conversation_messages` this way, including the one that decides what the model sees as recent turns and what compaction folds. Observed: `covers_through_message_id` pointed at the wrong message — **compaction folded the wrong turns**. Fixed by making `utcnow()` strictly increasing. Two "flaky" tests were never flaky; the suite now passes across random orderings. |

---

## Tasks 2–16 — consolidated findings

Findings from the implementation and review of Phase 0, Phase 1 and Phase 2.

### Confirmed defects, found and fixed

| # | Severity | Where | Finding |
|---|---|---|---|
| F1 | **Critical** | `app/chat/agent.py` | **`asyncio.gather` over tool calls broke 3 of every 4.** Every executor shares one `AsyncSession`; SQLAlchemy refuses concurrent operations on one ("This session is provisioning a new connection"). Measured with four calls: the first succeeded, the other three returned "could not be completed" on perfectly good data. The user-visible effect would have been the assistant claiming a reader's records were unavailable whenever the model asked for more than one thing at once — exactly what the tool design encourages. **Fixed:** sequential execution; regression test fails if `gather` returns. |
| F2 | **High** | `app/chat/tools/registry.py` | `json.dumps` sat OUTSIDE the try block, so a non-serialisable payload escaped the registry's never-raise contract entirely and propagated into the loop. **Fixed:** serialization moved inside the failure boundary. |
| F3 | **High** | `app/chat/orchestrator.py` | **The trace echoed the banned phrase back to the client** — `blocked (banned:you probably have)`. The reply was correctly withheld while the trace quoted it verbatim. **Fixed:** `redact_reason()` gives the trace the category only; the corrective retry still receives the specific phrase because it needs it. |
| F4 | **High** | `app/chat/orchestrator.py` | The agentic path had **weaker numeric safety than legacy**: with no tools called and nothing retrieved, `sources` is empty, so `values_traceable` had nothing to compare against and an invented dose passed. **Fixed:** `ungrounded_value` policy — a dose stated when nothing was retrieved and no tool ran has nothing behind it. |
| F5 | Medium | `app/grounding/fidelity.py` | `%` is a non-word character, so a trailing `\b` after it demanded a following word character and `6.1%.` never matched — the guard silently missed percentages at the end of a sentence. **Fixed:** percentages get their own alternative. |
| F6 | Medium | `app/chat/episodes.py` | SQLite returns NAIVE datetimes for a `DateTime(timezone=True)` column while PostgreSQL returns aware ones; comparing them raises. Exactly the cross-backend gap drawbacks §8.4 describes. **Fixed:** `_aware()` normalisation. |
| F7 | Medium | `tests/test_pg_hybrid_retrieval.py` | A test called `can_view_document` with an invented signature. The `pg` skip was hiding it, so it would have failed in CI on the first real run. **Fixed:** verified against the real signature. |
| F8 | Low | `app/chat/agent.py` | A comprehension variable shadowed the `AgentOutcome` named `outcome`. Python scopes it safely, so it was not a live bug — but it is the line someone misreads at 3am. **Fixed:** renamed. |

### Process failure

| # | Severity | Finding |
|---|---|---|
| P1 | **High** | **Review agents were given write access to the tree they were reviewing.** They left nine scratch test files that monkeypatched global state without cleanup. With those files present the **emergency-ordering tests failed** — the most alarming possible signal from this suite, and pure artifact. One agent also left `return None  # MUTANT` in `executors.py`, disabling lifestyle logging entirely. Verified it never reached a commit. **Resolution:** reviewers are read-only from here on. A reviewer that mutates the tree can make working code look broken and broken code look fine. |
| P2 | Medium | One commit went in with ruff and pyright errors **visible in the same terminal output**. Verification has to be READ, not merely produced. Fixed in the following commit. |

### Plan defects found before coding

| # | Finding | Resolution |
|---|---|---|
| A7 | Task 11 called for `asyncio.gather` over the per-turn lookups. Impossible for the same reason as F1 — one shared session. | Rewritten as deduplication: memoised patient context, removed a duplicate `resolve_scope`, registry TTL. |
| A8 | Task 5's executors read `provenance` keys the handlers never emitted (`value`, `unit`, `recorded_at`); `handle_metric_query` formats them into the reply string and discards the structure. | Widened the handler's provenance first, minimally, using the values already bound on every path. |
| A9 | Task 7's acceptance gate would fail for an unrelated reason: two eval scenarios pin `path` to legacy handler names the agentic engine cannot produce. | `run_evals` is engine-aware — a scenario pins BEHAVIOUR, not the engine. |

### Deliberately not fixed

- **`FakeProvider.DEFAULT` wording.** Load-bearing: it contains no "clinician",
  which is how the outage evals distinguish a degraded safe reply from a model
  answer. Rewording it makes that scenario vacuous.
- **Extra tool calls in a bake-off case.** Calling something additional is
  untidy, not wrong; missing a required one is wrong. Scored accordingly.
- **The `(end_turn, tool_calls present)` row of `wants_tools`.** Genuinely
  debatable for OpenAI-compatible gateways, which return `finish_reason: "stop"`
  with a populated `tool_calls` array. Pinning it now would cement a possible
  adapter bug as intended behaviour.

---

## Phase 3 — Tasks 17, 18, 19

**Review:** 17 agents, four lenses (access control, vision, voice,
adapters/tests), each finding put through adversarial refutation. **58 findings
raised, 3 survived refutation as stated** — but the verdict pass upgraded
several the per-finding verifiers had softened, and all of the below were
confirmed against the real code before being fixed.

**Reviewers were read-only this round.** No scratch files, no mutations, no
false signals.

### Critical

| # | File | Finding |
|---|---|---|
| C1 | `app/api/v1/chat.py` | **The low-confidence voice branch returned a reply without running the triage floor, and hardcoded `risk_level=NONE`.** Verified live: `triage("I can't breathe")` → emergency, `triage("I want to kill myself")` → emergency; both returned HTTP 200 with `risk_level="none"`, no escalation copy, and the Tele-MANAS helpline withheld. This was the only path in the codebase producing a user-visible reply with the floor unrun — and hardcoding NONE is not "the floor did not run", it is *lowering* it. The asymmetry ran the wrong way: ASR confidence collapses on breathless, slurred, panicked or pained speech, so the gate fired hardest on the population it most needed to protect. **Fixed:** the floor runs on what was heard, always; a red flag escalates first and the confirmation is appended after. |

### High

| # | File | Finding |
|---|---|---|
| H1 | `app/api/v1/chat.py` | The confirmation prompt quoted raw ASR output to the reader without `validate_reply`. Verified: a transcript of "you probably have dengue" and one naming a provider both shipped verbatim. ASR output is a model's guess — untrusted from the same direction as vision output, which this same work routes through the validator. **Fixed.** |
| H2 | `app/chat/conversation.py` | `ensure_session` returned any existing row by id **without checking who owned it**. Passing another user's `session_id` loaded their history into your prompt and appended your turn to it. Pre-existing since Task 16, widened by Phase 3's third caller. **Fixed once in the shared function** — smaller than four endpoint guards, and covers `/chat`, `/chat/stream`, `/chat/upload`, `/chat/voice`. |
| H3 | `app/chat/agent.py` | Vision text flowed into `sources` for the numeric-fidelity guard, so an OCR misread would be **authorised** by the one guard that exists to catch it. Also: nothing told the vision model that text inside an image is data rather than instruction, and the transcript lands in a tool result the main model reads — a model that can call tools. **Fixed:** `ToolResult.trusted_values`, and an explicit injection rule in the prompts. |

### Medium

| # | Finding |
|---|---|
| M1 | The transcript bypassed the 4000-char bound every other text entry point enforces. 10 MB of Opus is 50+ minutes of speech, persisted whole and injected whole into the prompt. **Fixed.** |
| M2 | `audio` was an unbounded string; the 10 MB cap was consulted *after* decoding. **Fixed at the schema**, with the post-decode check kept as the exact backstop. |
| M3 | `confirmed` was an unbound client boolean that **disabled the gate entirely** when sent on the first request — and confirming re-ran a sampling ASR decoder, so the text acted on need not be the text the reader saw. **Removed the field and the branch**; a client that agrees posts the text to `/chat`. |
| M4 | `MAX_BYTES` was enforced *after* httpx buffered the whole body — a ceiling on what was returned, not what was read. The module comment claimed otherwise. **Fixed by streaming** with an incremental check; a lying `content-length` gets no free pass. |
| M5 | `VISION_MODEL` was read in exactly one place, a truthiness gate. Pointing it at a multimodal endpoint did nothing but flip a boolean. **Fixed:** it now selects a real provider. |
| M8 | **The family branch of the consent gate had zero coverage** across 1559 tests — every test used `viewer == owner`, which short-circuits on the first line of `can_view_document`. The four-condition gate that is the entire reason the module is careful was never exercised. **Five tests added**, including the wrong-side grant and the per-file exclusion. |

### Low

| # | Finding |
|---|---|
| L4 | A remote header went unbounded into the audit column. **Fixed** with `[:64]`. |
| L5 | An attachment message with no text emitted an invalid empty text block. The sibling branch fifteen lines below already guarded this. **Fixed in both adapters.** |
| L6 | `VoiceSidecar.synthesize` has no caller and `ChatResponse` has no audio field. **Documented honestly** rather than left implying it is wired. |
| L7 | **Three tests that could not fail.** The non-http URL test passed with the guard removed (the stub raised on the next call, the bare except caught it, and the function returned `None` — the exact assertion). The transport test raised on the *first* call, exercising the URL handler rather than the byte handler its name claimed. And the only test inspecting what `describe_image` sends asserted the text alone — an implementation that dropped the image passed all 20 tests in the file. **All three now pin what they claim.** |
| — | `startswith("http")` also accepts `"httpfoo://"`. Tightened to an exact scheme check. |

### Refuted (verified as *not* defects — do not re-litigate)

- **`owner_id`/`is_private` are caller-asserted, so any id passes** — the
  `analyze_image` wiring resolves both from the row via `document_owner()`
  before the fetch. Keep it that way.
- **The `kind` fallback defeats per-file exclusions** — `_RESOURCE_TYPE`'s six
  values are exactly the enum members, so every resolvable value maps to itself.
- **`startswith("http")` is an SSRF primitive** — no attacker-controlled input
  reaches the signed URL, the byte fetch sends no headers, and redirects are off
  by default. Tightened anyway as free hardening.
- **Sharing the injected client leaks the Spring token to the storage host** —
  every production path opens a fresh client per leg; the token is a
  per-request header, never a client default.
- **`_record`'s bare except poisons the session** — mechanically true in
  isolation, but `execute_tool` wraps executors in a SAVEPOINT that neutralises
  it. The proposed `await db.rollback()` would have discarded the whole turn.
- **`UserMessage` frozen-with-mutable-dict** — nothing hashes it and nothing
  mutates an attachment; both adapters serialise immediately.

### Deferred, deliberately

- **`job_runs` has no actor column.** You learn a document was read, never by
  whom — the one field an access-control audit exists for. `job_runs` is
  Flyway-owned, so adding `user_id` needs a migration adopted into mhn-spring
  plus a coexistence re-check. Worth doing; not worth doing quietly.
- **Audit durability.** `_record` writes inside the caller's transaction, so a
  tool failure after a successful fetch rolls the audit row back while the read
  already happened. Fixing it properly means a separate short-lived session.
- **`is_private=None` is treated as shareable.** Consistent with the listing
  filter and documented production semantics, but worth a deliberate decision
  now that the consequence is bytes rather than a listing row.

# Davi backend — audit

**Produced 2026-09-01.** Supersedes `project_docs/drawbacks.md` (that document described the state at the end of Phase 4; this one replaces it as the standing risk register).

## How this was produced

Four auditors worked the repo in parallel — clinical safety and guard rails, security and data exposure, data integrity and schema, reliability and operability, plus a product/missing-feature pass. Every claim was then re-derived by an adversarial verifier that ran the real modules (probe scripts, `triage()` and `handle_chat` executed against aiosqlite, `validate_reply` and `analyze_grounding` called directly) rather than reading code and inferring. A live session drove the deployed app for the paths a unit test cannot reach. Findings that did not survive verification were deleted; findings whose mechanism was right but whose consequence was inflated were kept at a corrected severity. **Every severity below is the post-verification one.**

## How to read the severities

Severity is *consequence × reachability*, not effort. `critical` = reachable today on a default deployment with a serious consequence. `high` = reachable but gated behind a fault, a misconfiguration, or a non-default flag; or a live defect with a bounded blast radius. `medium` = real, unfixed, with a mitigation already in place. `low` = defence-in-depth, hygiene, or a documented tradeoff worth revisiting. Items already tracked in `project_docs/open-items.md` are named by ID and not restated.

---

> ### Corrections applied after this document was generated
>
> Two findings were re-measured by hand against the real modules before
> publishing, and both moved:
>
> * **C1 is overstated as written.** `triage("my face is drooping and my arm
>   feels weak and i cant speak properly")` returns **emergency**, as does
>   "sudden weakness on one side" and "i think im having a stroke". What
>   actually misses is an idiomatic variant — "one side of my face has gone
>   droopy and i cant lift my arm" returns **none**. The finding is real and
>   the mechanism is right; the headline "a textbook FAST description returns
>   none" is not. Treat C1 as **high**, not critical, and read it as
>   *phrasing coverage is uneven*, not *the floor misses strokes*.
> * **C6 was real and is now FIXED** (commit following this document). The bare
>   phrase `"been cutting"` matched "I have been cutting down on sugar":
>   `self_harm=True`, `level=emergency`. A diet question returned the self-harm
>   crisis reply with a helpline, and — after the episode floor shipped — an
>   EMERGENCY episode pinning every later turn to seek-care for 14 days. A
>   narrow `_CUTTING_IDIOM_RE` now strips "cut down/back on|out" before the
>   self-harm scan; every real disclosure still fires. Pinned by
>   `tests/test_episode_floor_and_cache.py`.
>
> Nothing else in this document has been hand-verified beyond the automated
> adversarial pass. Re-measure before acting on any single finding — that is
> the lesson C1 and C6 both teach.

## Executive summary

| # | The thing | Section |
|---|---|---|
| 1 | The deterministic triage floor is written in clinician idiom, so some ordinary phrasings of a stroke miss it. **Corrected after re-measurement — see the note in C1: a textbook FAST description DOES fire; an idiomatic variant does not.** | [Clinical safety → C1](#c1-the-triage-floor-does-not-speak-patient-english-critical) |
| 2 | The startup guard that refuses an unsafe deploy is keyed on `APP_ENV`, which defaults to the unsafe value and appears in no deploy descriptor. | [Security → S1](#s1-the-refuse-to-start-guard-is-keyed-on-a-setting-that-defaults-to-the-unsafe-value-high) |
| 3 | Three of the four output guards are thinner than the architecture implies: the validator blocks almost nothing at NONE, the HIGH escalation check grades a banner the orchestrator just prepended, and grounding defaults to `log`. | [Clinical safety → C3, C4, C7](#clinical-safety) |
| 4 | Erasure and retention have one executor, it cannot prove it has ever run, it marks itself `succeeded` before doing the destructive work, and one failing account jams the whole queue. | [Correctness → D1, D2](#correctness-and-data-integrity) and [Reliability → R3](#r3-the-sweep-cannot-prove-it-has-ever-run-medium) |
| 5 | The production LLM call has no timeout (SDK default: 600 s read × 3 attempts) and the fail-open guards swallow DB errors without rolling back, turning a partial failure into a 500. | [Reliability → R1, R2](#reliability-and-operability) |

Runner-up worth knowing: **Davi reads the reader's family history and never reads the reader's own** conditions, surgeries or non-medication allergies ([Missing features → M5](#m5-the-readers-own-diagnoses-surgeries-and-non-medication-allergies-are-never-read-high)).

---

## Security

### Anything that lets data cross between users

#### S1 — The "refuse to start" guard is keyed on a setting that defaults to the unsafe value (high)

`app/config.py:102` (`app_env: str = "dev"`), `:35-36`, `:153-166`

**Mechanism.** `_no_open_door_outside_dev` — whose docstring says "A misconfigured deploy must die at startup, not serve" — runs only `if self.app_env != "dev"`. `app_env` defaults to `dev`, `auth_enabled` to `False`, `jwt_secret` to `change-me-in-prod`. `APP_ENV` appears in no deploy descriptor: `railway.toml:37-56`'s "Required environment" block and `docs/production_integration.md:29-37` both list `AUTH_ENABLED`, `JWT_*`, `SERVICE_TOKEN`, `LLM_*`, `GROUNDING_MODE`, `EMBEDDING_*` and never `APP_ENV`. Compounding: `get_settings()` is called lazily from request handlers, never from the app factory, so even when armed it raises on first request rather than at boot.

**Consequence.** The primary control (`AUTH_ENABLED=true`) is documented in three places, so this is a *backstop that is inert by default*, not a live hole. But if `AUTH_ENABLED` is ever lost — a service re-create, a copied env, a preview environment — the app starts happily and `app/auth.py:108-117` takes identity straight from the `X-User-Id` header, on a service sharing the MHN production database. `curl -H 'X-User-Id: <uuid>' /api/v1/chat/sessions/<id>/messages` returns that user's transcript; `PUT /pedigree` rewrites their family history; `DELETE /profile` schedules erasure.

**Fix.** Default `app_env` to `"production"` (or make it required with no default) so an unset value fails closed; add `APP_ENV` to `railway.toml`'s required list; call `get_settings()` once in `create_app()` so a bad config kills the boot instead of every request.

#### S2 — One static `SERVICE_TOKEN` both impersonates any user and unlocks admin, with no record that impersonation happened (medium)

`app/auth.py:123-139`, `app/api/v1/admin.py:42-58`

**Mechanism.** The same secret satisfies two different checks: in `get_current_user_id` it makes `X-User-Id` authoritative for any user on every route; in `require_service_token` it is the admin credential. No scope, no token id, no route allowlist. Nothing marks an impersonated action afterwards — `job_runs.actor_user_id` (`app/documents/fetch.py:126`) and `insight_review_audit.reviewer_user_id` record the *claimed* user.

**Consequence.** The BFF that legitimately holds the impersonation token also holds admin. A leaked value is read/write across every user, attributed in the audit trail to the victims. (Not as bad as it first looks: `/admin/sweep` can only execute erasures already past their grace window, and the clinician routes still check the roster.)

**Fix.** Split `ADMIN_TOKEN` from `SERVICE_TOKEN`; stamp an `actor_kind`/`via_service_token` flag on `job_runs` and any audit row written on the service path.

#### S3 — The per-file consent opt-out is the only condition in its gate that fails open (medium)

`app/coredata/service.py:329-347`, consumed at `:387` and `:406-410`

**Mechanism.** `_viewer_exclusions` wraps the `file_access_exclusions` read in a bare `except Exception: return {}`, and `can_view_document`'s last line is `return resource_id not in denied.get(resource_type, set())` — so `{}` means *allowed*. Owner match, `is_private`, accepted connection and owner-side read grant all return `False` on doubt; only the finest-grained control, where an owner said "not this file, not this person", turns a failure into a grant.

**Consequence.** Narrower than it reads. A poisoned session dies on the un-wrapped sibling query first, and document *bytes* are gated a second time by Spring's authoritative `assertCanRead`. Residual exposure is document listing metadata (kind, title, date) for an excluded file during a transient single-statement fault.

**Fix.** Narrow the except to the missing-table error the comment is actually about (or probe once at startup) and re-raise everything else.

#### S4 — `fetch_ai_result` takes no viewer identity, and one of two id-resolution branches skips the ownership check its sibling performs (low)

`app/documents/service.py:212`, `app/chat/data_handlers.py:1060-1076`

**Mechanism.** `fetch_ai_result(document_id)` calls mhn-ai with Davi's service token and no user context — mhn-ai performs no user-level authorization (stated at `service.py:109`). The filed-document branch checks `doc.user_id == user_id`; the `unclassified` branch takes the id straight off a persisted message card. Not currently exploitable: `ensure_session` refuses a foreign session id, `extracted_intent` is server-written only, and the only producer of `unclassified` cards is a user-scoped query. The asymmetry is also load-bearing, not accidental — the `unclassified_files` row is deleted the moment mhn-ai files the document, which is exactly when the reader asks.

**Fix.** Give `fetch_ai_result` a `viewer_id` and resolve ownership inside it, so no future caller can get it wrong.

#### S5 — Reviewer standing is one global boolean; `/review/audit` is unscoped (low)

`app/api/v1/review.py:322`, `:66-87`

**Mechanism.** `GET /review/audit` with no `subject_user_id` selects across every reviewer and every subject. `ClinicianReviewer.active` is the only gate; there is no notion of which patients a clinician is responsible for. Bounded by design: no `offset`/cursor on either that route or `/review/queue`, so a caller sees the most-recent 500 rows and the oldest 200 held artifacts, not a paged enumeration. The marginal exposure over what the same caller can already read is peers' free-text `note` plus a who-viewed-what index. Already reasoned about — `findings-phase-4.md` R-4/R-6 made the unscoped read audit itself.

**Fix.** Scope when a patient-panel data source exists; until then, redact `note` for reviewers who are not its author.

#### S6 — A "Davi Test Console" that demonstrates header impersonation is served at `/` in every environment (low)

`app/main.py:46-49`, `ui/index.html:224-235`

**Mechanism.** `if _UI_INDEX.exists()` is the only gate — no `app_env` check, unlike `app/api/v1/documents.py:96`, which does exactly that for its dev-only preview. `Dockerfile:17`'s `COPY . .` bakes the file in, so the condition is permanently true. The page hardcodes six seeded persona UUIDs and sends `X-User-Id` with no bearer token.

**Consequence.** With auth on it is inert — an information leak of six repeated-digit synthetic UUIDs and a header name, on a host that already serves `/openapi.json` unauthenticated. It becomes a point-and-click cross-user browser only if S1's double misconfiguration happens.

**Fix.** `if _UI_INDEX.exists() and get_settings().app_env == "dev":`.

### Auth and secrets

| Finding | Where | Mechanism → consequence | Fix |
|---|---|---|---|
| **S7** JWT accepted with no `exp` requirement, no `iss`/`aud`/type binding, no revocation or user-existence check (low) | `app/auth.py:141-165` | `jwt.decode` is called with no `options`; python-jose validates `exp` only when present, so an exp-less token never expires, and `sub` is never checked against a live account. Bounded by the pinned alg (HS512 — Spring's OTP/OAuth challenge tokens are HS256 with no user `sub`) and by `authorize_user`, so there is no cross-user path; the real gap is that a deactivated account keeps working until its own expiry. | `options={"require": ["exp","sub"]}`, agree an `iss`/`aud` with mhn-spring, and one indexed `SELECT 1 FROM "user" WHERE id = :sub`. |
| **S8** All seven secrets are plain `str` (low) | `app/config.py:36, 42, 48, 109, 119, 128, 139` | No `SecretStr` anywhere. Verified there is **no live disclosure path** — pydantic truncates `input_value`, no APM is installed, `create_app()` sets no `debug`, and `app/db.py:38` already sets `hide_parameters=True` for exactly this threat. Pure defence-in-depth against a future careless `repr(settings)`. | `SecretStr` on all seven, `.get_secret_value()` at the four use sites. |
| **S9** The presigned URL from Spring is fetched on a scheme check alone, and a schemeless base URL defaults to `http://` (low) | `app/documents/fetch.py:194`, `:95-99`; also `app/documents/service.py:51-55`, `app/medicines/service.py:66`, `app/medicines/adherence.py:75` | No host allowlist on the streamed URL; `follow_redirects` is off, no credentials ride the presigned GET, and the content type is allowlisted, so the SSRF requires an attacker who already owns Spring — the auth authority on the same private network. The `http://` default is for Railway-internal hosts, not a downgrade of an `https://` value. | Allowlist the presigned host; refuse a schemeless base unless the hostname is a known private one. |

### Privacy and consent

#### S10 — Revoking personalization consent does not clear the derived memory document (medium)

`app/chat/profile.py:210-242` (`forget_everything`)

**Mechanism.** `revoke_personalization` appends the ledger row and deletes three tables — `user_profiles`, `user_memories`, `turn_feedback`. It does not delete `user_memory_document`, whose `prompt_block` is the rendered profile including conditions and medications (`app/memory/document.py:235-240`). `app/chat/memory_assembly.py:123-127` serves that block verbatim with **no consent check at all**, short-circuiting the live path where `render_for_prompt` would have gated on `view.has_consent`. `app/chat/erasure.py:65` already classifies this table as a per-user copy that must be destroyed — the revoke path simply never got the same treatment.

**Consequence.** Two windows: for up to an hour after the nightly build the revoked profile still reaches the model, and the JSON copy of the revoked conditions/medications persists at rest indefinitely (forever, for a user with no pedigree data, since nothing ever rebuilds it). The function's own docstring says it exists to stop "the ledger would say 'no' while the rows said otherwise".

**Fix.** One line: `delete(UserMemoryDocument)` beside the other three. `tests/test_profile.py::test_revocation_also_erases_the_stored_data` currently passes with the document intact.

| Finding | Where | Mechanism → consequence | Fix |
|---|---|---|---|
| **S11** `job_runs` survives a completed erasure and has no retention (low) | `app/chat/erasure.py:51-66`, `app/documents/fetch.py:117-129` | Every fetch writes `actor_user_id` + the plaintext resource string (`"reports:123"` — despite the column being named `input_hash`). `job_runs` is not in `_ERASE_IN_ORDER` and not in the retention sweep. **This is a documented decision, not an oversight** — `app/models/jobs.py:30-38` and open-item **A8** both state the row must outlive the account it attributes. What is genuinely wrong: `purge_user`'s docstring claims "every Davi-owned per-user row", and the table has no window at all while the sweep appends to it nightly. | Correct the docstring; give the table a window longer than the 400-day receipt window. |
| **S12** `/metrics` is unauthenticated and mounted twice (low) | `app/api/v1/health.py:18-35`, `app/main.py:34-35` | No dependency, no middleware. Series are PHI-free with bounded labels, but they disclose turn volume by risk, token spend, degradation reasons and clinician-access counts. The docstring's mitigation ("keep it off the public internet") *is* available — `docs/production_integration.md:198` shows this project already runs a domain-less private-only service. | One-line `require_service_token` dependency, or drop the bare mount. |
| Receipts store the offending **generated** sentence verbatim and can echo the reader's own numbers | — | Tracked as **C11**. Still needs your decision (drop `sentence`, hash it, or correct the two docs that claim receipts are PHI-free). | — |

---

## Clinical safety

### The bug class, which is what recurs

Three of this repo's worst defects share one shape: **a deterministic safety behaviour that only fires for the phrasing, the engine, or the entry point its author was looking at.**

1. **Bypassed by phrasing.** The triage tables, the banned-phrase list and the ACS co-occurrence rule are substring matches over the words a clinician would write. A reader who says the same thing differently — "numb on one side", "chest discomfort", "struggling for air" — walks past the floor entirely. Tests assert the phrases that *are* in the table, so the hole is invisible from a green suite.
2. **Bypassed by engine.** The drug-interaction refusal sat inside the legacy branch and the agentic engine answered interaction questions from the model's weights. That one is fixed; **ten sibling step-4/step-5 handlers have never been audited for the same problem** — open-item **C8**, the prerequisite for **A2**. The whole grounding layer is a second instance (C8 below), and `scripts/run_evals.py` structurally cannot see this class for any scenario that pins only `path` (C-tail).
3. **Bypassed by entry point.** The A6 fix — record the episode and write a receipt *before* the emergency return — lives inside `handle_chat`'s shared prologue. `/chat/voice` never calls `handle_chat` on that branch, so on the voice path A6 does not hold (C5).

Any new deterministic, safety-relevant behaviour belongs in the shared prologue *and* needs one eval scenario asserting behaviour (not `path`) on both engines. That is the cheapest structural defence against all three variants.

### Findings

#### C1 — The triage floor does not speak patient English (critical)

`app/triage/red_flags.py:36-176`

**Mechanism.** `EMERGENCY_PHRASES`/`HIGH_PHRASES` are case-insensitive substring matches over clinician idiom. Executed against the real `triage()`, all of these return `level=none, matched_terms=[]`: *"my face is numb on one side and I can't speak properly"*, *"sudden weakness in my right arm and leg"*, *"I woke up and can't move my left side"* (the only stroke rows are "one-sided weakness"/"face drooping"/"slurred speech"); *"my tongue is swelling after eating peanuts"*, *"I've been stung and my throat feels tight"* (anaphylaxis is four throat-specific rows); *"high fever and I'm shivering uncontrollably and confused"*; *"my baby is floppy and not feeding"*, *"a rash that doesn't fade when pressed"* (no paediatric rows at all); *"I'm 30 weeks pregnant and bleeding"*, *"my baby hasn't moved all day"* (no obstetric rows); *"he is having a fit"* ("fit" is the everyday Indian/British word; the table has only "seizure"/"convulsion"); *"I can barely breathe"*, *"I'm struggling for air"* (the pattern at `:220` requires the literal stem `breath`). So do *"call an ambulance"* and *"this is an emergency"*.

**Consequence.** Verified end to end with `handle_chat`: all four sampled messages returned `risk_level=none`, `recommended_action=discuss_with_clinician`, `path=symptom_rag`, provider text passed through verbatim — no banner, no emergency directive, no reassurance block (validation only engages at HIGH/EMERGENCY). Stroke and anaphylaxis are the two conditions where the floor's entire reason to exist is minutes-to-treatment.

**Fix.** Two changes, not one more phrase. (1) Expand the tables from patient corpora — `evals/questions_10k.csv` and `evals/live_sweep_127_staging_*.json` already contain real phrasings. (2) Add a symptom-combination tier alongside the existing ACS rule at `:180-207`: one-sided {weakness|numbness|droop|can't move} + {face|arm|leg|speech}; {tongue|lip|face|throat} + swelling + {sudden|after eating|sting}; fever + {floppy|non-blanching|won't feed}. That mechanism is already proven at `:382` and generalises where a phrase list cannot.

#### C2 — "chest discomfort" is not a chest symptom, and "nauseous" is not "nausea" (high)

`app/triage/red_flags.py:254` (`CHEST_STEM_RE`), `:201` (`ACS_ASSOCIATED_PHRASES`)

**Mechanism.** `CHEST_STEM_RE` lists `pain|hurt|ach|tight|heav|pressure|burning|squeez` but not `discomfort` — the standard clinical word for atypical ACS. Measured: `triage("chest discomfort with sweating for the past hour")` → `level=none, matched_terms=[]`, even though `assoc_hits=['sweating']`; the co-occurrence rule fails against its own stated intent for want of one stem. Separately, substring matching does not reach "nauseous", so `"my chest pain started 3 days ago and I also feel nauseous"` → `high`, not `emergency`.

**Consequence.** No banner, no reassurance block, straight to the model at `risk=none`, for a message naming both halves of the rule. (The related claim that "crushing chest pain" is under-tiered does *not* hold as a defect: it fires HIGH with a mandatory "seek medical care promptly" directive, and whether bare severe chest pain should be EMERGENCY is a deliberate, commented, tested clinical decision at `:385-388` awaiting sign-off.)

**Fix.** Add `discomfort|heaviness` to `CHEST_STEM_RE`; stem the ACS associates (`nause\w*`, `sweat\w*`, `breathless\w*`). Two tokens.

#### C3 — At NONE risk the output validator blocks almost nothing clinically dangerous (high)

`app/chat/validation.py:85-111` (`_diagnostic_pattern`), `:152-168`, `:286`

**Mechanism.** Both diagnostic branches anchor on the literal `you have` / `you are` / `you're`. Executed against `validate_reply(text, "none")`, all of these **pass**: *"This sounds like dengue."* / *"That's classic appendicitis."* / *"Your symptoms point to a heart attack."* / *"I'd say it's a kidney stone."* / *"You can stop taking your metformin once your sugar is normal."* / *"Stop the antibiotic once you feel better."* / *"Double your dose if the pain doesn't settle."* / *"There's no need to go to hospital for this."* Only *"It looks like you're diabetic"* is caught. `extra_conditions` (the 512 registry names) cannot help — it widens the condition list *inside those same templates*. `discourages_care()` works, and is invoked only `if risk_level in (HIGH, EMERGENCY)`.

**Consequence.** NONE is the risk level of most turns. The stated invariant is "never diagnosis — enforced in code"; in code the enforcement is two string templates. Grounding does not cover it either: seven of eight dangerous sentences come back `grounded`, the imperative med forms clear `_DIRECTIVE_RE`, and the code default is `log` (C7).

**Fix.** Three cheap additions. (1) A diagnostic-framing alternation: `(?:this|that|what you(?:'re| are) describing)\s+(?:sounds like|looks like|seems like|is consistent with|points to|is classic)` + the condition lexicon. (2) A medication-directive family — the shape already exists as `_DIRECTIVE_RE` in `app/grounding/claims.py:62`, reuse it rather than writing a second. (3) Run `discourages_care()` at every risk level.

#### C4 — The HIGH escalation check grades a banner the orchestrator just prepended (high)

`app/chat/orchestrator.py:1050`, `:1190`, `:1668`; `app/chat/validation.py:292`

**Mechanism.** All three engine paths do `display = f"{escalation} {display}"` *before* validation. `HIGH_ESCALATION` (`app/chat/replies.py:82`) contains "seek medical care promptly" and "urgent care", both in `_ESCALATION_MARKERS`. `validate_reply`'s HIGH branch then asks `has_escalation(reply)`, which the prepend guarantees. Measured: `validate_reply(HIGH_ESCALATION + " This sounds like dengue.", "high")` → PASS; same for a heart-attack diagnosis and two medication-stop directives. Without the banner all four correctly BLOCK — which is what the tests measure, and why this reads as working.

**Consequence.** `missing-escalation`, one of the two stated non-negotiable HIGH invariants, cannot fire on real model output on either engine. Combined with C3, a HIGH-risk chest-pain turn can carry a confident diagnosis and a medication-stop directive under an urgent-care banner and be logged as having passed every safety check.

**Fix.** Validate the model's text, then prepend. A one-line reorder at each of the three sites, plus `_try_recover` at `:1697`.

#### C5 — A spoken emergency leaves no transcript, no episode and no receipt (high)

`app/api/v1/chat.py:471-503`

**Mechanism.** The voice endpoint runs `triage(transcript.text)` itself and, on EMERGENCY/HIGH, returns a `ChatResponse` after nothing but `ensure_session` + `commit`. It never calls `handle_chat`, so the shared prologue's A6 record-before-exit (`orchestrator.py:718-731`) never runs, no `conversation_messages` row is written, and `_write_receipt` is never called. The same ternary is duplicated at `:474-478`, so the self-harm/emergency suppression in C9 applies here too.

**Consequence.** The reader gets the correct directive and then the event vanishes. Their next turn ("it's getting worse") is answered as a first mention, because the carried-escalation machinery reads only episodes written by `memory_assembly.record`. An operator investigating an incident finds an empty session. `tests/test_voice_endpoint.py` asserts only the response body, so nothing catches it.

**Fix.** Lift the three calls out of `handle_chat`'s prologue into a helper and call it from both voice branches, plus `add_message` for the transcript and the directive.

#### C6 — The self-harm table fires on "I have been cutting down on sugar" and opens a 14-day EMERGENCY episode (high)

`app/triage/red_flags.py:281`

**Mechanism.** `SELF_HARM_PHRASES` contains the bare substring `"been cutting"` — the only entry in that table lacking a self-reference anchor (its siblings all carry `myself` / `my wrist` / `die` / `my life`), and the table has no negation guard, unlike `_CHEST_NEGATION_RE` at `:244`. Measured live on `handle_chat`: `risk_level=emergency`, `self_harm=True`, `provenance={'path':'triage_emergency','matched':['been cutting']}`, and the Tele-MANAS 14416 suicide-crisis copy. Before returning, `orchestrator.py:718` writes an `ActiveSymptomState` row `('been cutting', 'emergency')`, which `open_episodes` keeps for `STALE_AFTER = 14 days` and which raises every subsequent turn's floor to HIGH with the "you mentioned something earlier" banner. "been cutting back on oil", "I have been cutting my sugar intake" and "I have been cutting down on smoking" all do the same.

**Consequence.** This is a diabetes-and-family-history product; that sentence is among the most probable in the corpus. It is alarming, it writes a false self-harm emergency into the reader's stored memory, and it is precisely how a real escalation banner gets trained into wallpaper.

**Fix.** Anchor it: `\bbeen cutting (?:myself|my (?:arm|wrist|leg|thigh)s?)\b` — `"cutting myself"` already covers the genuine case. While there, the same probe found these returning `none`: *"I want to kill my self"* (spaced), *"I don't want to wake up tomorrow"*, *"a plan to end things"*, *"life is not worth living"*, *"jump in front of"*.

#### C7 — `GROUNDING_MODE` defaults to `log`, and the only deploy descriptor that mentions it has it commented out (medium)

`app/config.py:98`; `railway.toml:50`; `docker-compose.yml:28`

**Mechanism.** `grounding_mode: str = "log"`. In log mode `_apply_grounding` (`orchestrator.py:259`) returns the model's answer unchanged and logs a WARNING; nothing is retried, nothing is replaced. `railway.toml:50` shows `GROUNDING_MODE=enforce` inside a `#`-prefixed documentation block, not a real env section; `docker-compose.yml` sets `log` explicitly; `.env.example:42` is the only place it is genuinely `enforce`. `implementation-plan.md:2056` folded "change the default to enforce" into Task 7, which shipped without it.

**Consequence.** Narrower than it first reads: the mode-independent fidelity ladder at `orchestrator.py:1194-1209` still catches ungrounded doses, reference ranges and BP pairs, and `validate_reply` still runs. The genuine residual is the non-numeric assertion class at `claims.py:62-101` — directives and prognoses — plus cited-content verification and `[GK]` misuse. Note the setting is moot on the agentic engine, which never calls `_apply_grounding` (C8).

**Fix.** Default to `enforce`, make `log` the opt-in, log a WARNING at startup when grounding is not enforcing while `AUTH_ENABLED` is true, and put the value in `railway.toml`'s real env block. Accept the cost `drawbacks.md:290` names: a second LLM call per violation.

#### C8 — The agentic engine runs no grounding at all (medium)

`app/chat/orchestrator.py:1326`, `:248/275`

**Mechanism.** `analyze_grounding` has exactly one caller, reached only from the legacy path at `:1171`. `_dispatch_agentic` calls `strip_markers`, then writes `grounding_status="agentic"` — a path label, not a pass/fail, so an auditor reading `rag_turn_receipts` cannot tell a clean agentic turn from a degraded one. Its only content guard is `values_traceable`, which by its own docstring checks unit-bearing values only. `assertion_kind`/`is_factual` have no callers outside `claims.py`.

**Consequence.** The directive family ("you can stop taking it once you feel better") is uncovered on the agentic engine. Partly mitigated: `validate_reply` runs on both engines and, at HIGH/EMERGENCY, catches two of the three prognostic examples. Also latent — `chat_engine` defaults to `legacy`. But Task 12 (**A2**) proposes deleting the legacy chain, which would delete the only caller and make the gap permanent.

**Fix.** Move the assertion check into a marker-free function (`assertion_kind` is already pure and standalone at `claims.py:90`) and call it on `display` next to `values_traceable` at `:1740`; record a real `grounding_status` on the agentic receipt.

#### C9 — Self-harm plus a physical emergency suppresses the medical directive (medium)

`app/chat/orchestrator.py:733`

**Mechanism.** `SELF_HARM_REPLY if tr.self_harm else EMERGENCY_DIRECTIVE` is a strict either/or. Measured: `"I want to die and I can't breathe"` → `self_harm=True` with both term sets in `matched_terms`, and only the mental-health copy is returned — whose routing is conditional ("or your local emergency number if you are in immediate danger") rather than the unconditional "go to the nearest emergency department right now". `red_flags.py:84-87` deliberately puts a disclosed ingestion in `EMERGENCY_PHRASES` with a comment — "a disclosed ingestion is a medical emergency first" — and that intent is defeated the moment the same message also carries intent phrasing: `"I took too many pills, I want to die"` suppresses the ED directive.

**Consequence.** The reader in the worst state the system can encounter gets the softer of two directives. Low frequency, maximum consequence, and both halves already exist as validator-clean audited copy. (`recommended_action` stays `call_emergency_services`, so a structured consumer still sees the emergency.)

**Fix.** When both fire, concatenate — `EMERGENCY_DIRECTIVE` then `SELF_HARM_REPLY`. Needs a clinician to sign off the ordering, which is the right kind of decision to escalate.

#### C10 — The medication WRITE path performs no allergy or duplication check, while the read path does (medium)

`app/chat/medication_flow.py:1000` (zero occurrences of "allerg" in 1,178 lines)

**Mechanism.** The confirm branch calls `perform_medication_write` after a yes/no and nothing else. The module never imports `medication_allergies` or `allergy_warning`, never reads existing courses to detect a duplicate composition, and consults `medicine_master` only for a spelling suggestion. Both engines funnel through the same function (`app/chat/tools/executors.py:404`), and because the flow sits in the shared prologue at `orchestrator.py:667` and returns before the engine, the memory document's allergy line never reaches a model on these turns either. Meanwhile *asking* about a drug does get an allergy line (`orchestrator.py:496`).

**Consequence.** A reader with a recorded penicillin allergy can say "add amoxicillin 500 twice a day", get "shall I add it?", say yes, and have it written with no warning. Two paracetamol-containing courses coexist unremarked. Calibration: the read path's warning is a *generic* "you have these on record" line, not a drug-specific cross-check (`app/coredata/service.py:1037` says so), and Davi is documenting a medication the reader already takes — the app's own Medications screen writes the same row with the same absence of checks. So this is a missing cheap flag, not a bypassed control.

**Fix.** Before `perform_medication_write`, fold `allergy_warning(await medication_allergies(...))` into the *confirmation question*, not the post-write reply — the reader must see it before saying yes. `composition1/2` exist on `medicine_master` for the duplicate check.

#### C11 — Reference ranges are adult-only and sex-blind, with a silent wrong-band fallback (medium)

`app/health/ranges.py:94`, `app/health/reference.py:164`, `:175`

**Mechanism.** `classify(metric_key, value)` takes two arguments — no age, no sex, no pregnancy. `RANGES` is one adult table (hemoglobin 12–17 for everybody, with a `note` that names the sex split and then does not act on it). The backend path is age-banded but degrades badly: an unknown DOB becomes `_DEFAULT_ADULT_AGE = 40`, and when no `ThpAgeRange` band covers the reader, `next((r for r in ranges if ...), ranges[0])` silently picks the **youngest** band. Worse than filed: `thp_age_range` *has* a `sex` column (V14; V18 seeds 38 male / 40 female / 199 'any' rows) which `app/models/coredata.py:442-462` does not map, so selection runs across all three sexes undifferentiated. Both substitutions are invisible in the reply and in the receipt.

**Consequence.** A man reporting hemoglobin 12.5 — anaemic by any adult male reference — is told it is "within the typical range… That's reassuring", then handed a note saying the male range starts at 13. There is no "I don't have a range that fits you" exit, which is the only safe answer for a child. (Two mitigations the original write-up missed: the pregnancy case errs *conservative*, since the adult floor of 12 sits above the pregnancy floor; and a parent asking about a child is blocked upstream by `_THIRD_PARTY_VALUE_RE` in `app/chat/abilities.py:522`.)

**Fix.** Map `ThpAgeRange.sex` and filter `sex IN (User.gender, 'any')`; return `None` instead of `ranges[0]` when no band covers the age; stop substituting 40 for an unknown DOB; thread age/sex into `classify` and decline to grade when nothing fits. Pairs with open-item **C15** (no body-temperature range exists at all).

#### C12 — The `think`-proximity patterns and six bare substrings escalate education and bereavement questions (medium)

`app/triage/red_flags.py:219-238`

**Mechanism.** `\bthink\b[^,.!?]{0,25}\bstroke\b` and its heart-attack twin fire on *"do you think stroke is genetic"* and *"I think my father's stroke was preventable"*. Bare substrings fire on *"my grandmother had a seizure disorder, is epilepsy genetic?"*, *"my uncle died by suicide, does depression run in families?"* (also `self_harm=True` → the crisis helpline instead of an answer), *"what are the signs of paracetamol overdose?"*, *"what is a thunderclap headache"*, *"does aspirin help in cardiac arrest"*, *"is choking hazard a risk for toddlers with grapes"*. The `collapsed` pattern at `:232` has lookaheads for "onto the sofa" and "laughing" but no historical guard, though the chest rule has one at `:244`.

**Consequence.** Those readers get `EMERGENCY_DIRECTIVE` and nothing else. Bounded, though: the canonical family-history framings all measure `none` — *"my father died of a heart attack, am I at risk?"*, *"my mother had a stroke at 60, what does that mean for me?"*, *"is epilepsy genetic?"*, *"does depression run in families?"* — so the header comment's stated intent at `:216-218` is largely honoured, and this fails safe. The bereavement case is the genuinely user-hostile one.

**Fix.** Drop the `\bthink\b` branches (the `(?:having|is having|might be having)` branch already carries the real signal) and extend `_CHEST_NEGATION_RE`'s historical guard into a shared `_HISTORICAL_RE` applied to the pattern tier and to `seizure`/`overdose`/`cardiac arrest`/`unconscious`/`choking`/`suicide`.

### Clinical safety — the tail

| Finding | Where | Mechanism → consequence | Fix |
|---|---|---|---|
| The enforce-mode grounding retry is graded by a weaker check than the answer it replaces (low) | `app/chat/orchestrator.py:275` vs `:248` | The first pass passes `chunk_texts`/`patient_text`, enabling `unsupported_value`; the retry passes neither, so `claims.py:207` gates that branch off entirely and the retry is written to the receipt as `grounded`. The fabrication case is caught anyway by the unconditional `values_traceable` backstop at `:1194`; what escapes is **marker misattribution** — a value that exists in chunk 2 cited as `[1]`, rendering the wrong source. | Pass the same two kwargs. |
| The scope decline drops a carried HIGH floor back to NONE (low) | `app/chat/orchestrator.py:694` | The off-topic branch is gated on `not tr.matched` — exactly the carried-episode case — and hardcodes `risk_level=NONE`, discarding the `risk` raised at `:628`. Every sibling return uses `risk_level=risk`. Reproduced: one off-topic message during an open emergency episode returns `none`/`out_of_scope`. No persistent state is corrupted; a client's risk banner flickers off for a turn. The repo already fixed this exact shape on the voice path (`app/api/v1/chat.py:466`). | `risk_level=risk`. One token. |
| 463 machine-authored i18n red-flag phrases are unreviewed and untested (low) | `app/triage/red_flags_i18n.py` | Spliced into the tables at `red_flags.py:116/175/305`, matched by the same substring rule, marked DRAFT pending native-speaker and clinician review, with **zero test coverage anywhere**. The short-token false-positive theory does not hold — cross-script collision is impossible, the shortest Latin-script row is 7 chars, and an empirical sweep across seven scripts found no false positive. The real defect is clinical over-breadth in ~3 rows: bare લકવો/ਲਕਵਾ/ಲಕ್ವ ("paralysis") fires EMERGENCY, whereas bare "stroke" is deliberately excluded because family history is a core flow. | A review task, not a code fix. Get native-speaker sign-off; narrow the bare paralysis rows to match the English tier's treatment. |
| The safety-eval set has no self-harm, dosing-refusal, stroke-presentation or overdose scenario (low) | `evals/scenarios.json` | 17 scenarios; none exercises the only path returning `SELF_HARM_REPLY`, the dosing refusal at `orchestrator.py:406`, or a plain-language stroke description. Mitigated: CI runs `pytest` in the same job immediately before `run_evals`, and `tests/test_agentic_orchestrator.py:86` drives the full self-harm turn and asserts "14416", so that regression cannot ship. **The dosing refusal genuinely has no coverage anywhere** — `build_dose_refusal` has zero references outside the orchestrator. | Add a dosing scenario and a self-harm scenario; add a plain-language stroke case, which will fail today (that is the point). |
| `run_evals` accepts `path == "agentic"` for any expected path (low) | `scripts/run_evals.py:76` | Under `CHAT_ENGINE=agentic` every `path` assertion is unconditionally satisfied. Proven inert by patching the greeting handler to never fire: still 17/17. But the harness is *not* blind to the bypass class — simulating the interaction refusal back inside the legacy branch produced `[FAIL] interaction_never_guesses`, 15/17, via engine-independent `reply_contains`. Only scenarios pinning nothing but `path` rot. | Drop `, "agentic"` from line 76 and have `_dispatch_agentic` record the handler it reached; add a behaviour assertion to `greeting_exact` and `red_flag_beats_tracker`. |
| The medication-flow reply is the one orchestrator return that carries model-extracted text and is never validated (low) | `app/chat/orchestrator.py:676` | `_extract_via_llm`'s JSON `name` is echoed after `_clean_name` only. Not unique — six other deterministic returns skip the validator, two of which splice dynamic content — and the flow runs only at `risk == NONE`, so the validator's HIGH/EMERGENCY teeth are inapplicable. **Do not wire it naively:** `_PROVIDER_LEAK_RE` is a bare word-boundary match on `claude\|opus\|sonnet\|haiku\|gemini\|llama\|grok`, so a medicine name colliding with one would blank a correct add-confirmation mid-transaction. | Validate the template, not the reader's own noun. |

---

## Correctness and data integrity

#### D1 — One failing account jams the entire erasure queue (high)

`app/chat/erasure.py:242-253`

**Mechanism.** The per-user loop catches and calls `await db.rollback()`. SQLAlchemy's `_restore_snapshot` expires **every** object in the identity map on an outermost rollback, so the remaining `ErasureRequest` objects still held in `due` are expired; the next iteration's `request.user_id` triggers a refresh — synchronous IO in an async context — which raises `MissingGreenlet`, caught by the same except, which rolls back again. Reproduced with 3 due requests and one simulated failure: `{'erasures_executed': 0, 'erasures_failed': 3}`, zero rows reaching `completed`. The comment at `:250` asserts the opposite ("one bad account must not stop the rest") and there is no test for the failure branch. Both sessionmakers set `expire_on_commit=False`, which is why the success path never exposes it.

**Consequence.** Every remaining erasure in that batch dies, silently, counted into a dict nobody reads. Head-of-line blocking is a tail case, not the base case — `purge_user` is bulk deletes with no inbound FKs, so the realistic trigger is transient (statement timeout, deadlock with mhn-spring, a recycled connection) and clears the next night. Nothing lies to the reader: the request stays `pending`.

**Fix.** Select ids up front into plain tuples (`select(ErasureRequest.id, ErasureRequest.user_id)`), or re-fetch inside the loop after each commit/rollback. Add a test that fails one account and asserts the others still complete.

#### D2 — The sweep marks itself `succeeded` before erasure and retention run, and records nothing at all when it fails earlier (high)

`scripts/nightly_sweep.py:73-74`, `:100`, `:103-111`, `:112-117`

**Mechanism.** `job.status = "succeeded"` and `finished_at` are set at `:73`; the only `commit()` is at `:100`; `execute_due` and `purge_expired` run *after* it. On failure the except sets `status="failed"` and `flush()`es — never commits — and both callers (`_main:124`, `app/api/v1/admin.py:177`) leave the session without committing on the exception path, so the UPDATE is rolled back and the committed `succeeded` row stands. Symmetrically, a failure before `:100` rolls away the flushed-only `db.add(job)` and **no row exists at all**. `admin.py:163` tells the operator "the sweep writes a row on entry and updates it on exit, so a caller polls that" — neither half is true, and because the entry row is never committed, a poller can never see `running`.

**Consequence.** The only status record for a background job doing batched destructive deletes on a database three services share is wrong in both directions. An empty `job_runs` cannot be distinguished from "never triggered" — which is exactly the ambiguity staging sits in, and exactly why this endpoint was written. This is open-item **C9**'s shape (audit rows not durable in the caller's transaction); the fix is the same short-lived session as **C7**.

**Fix.** Commit the `running` row immediately after `:45` in its own session; set `succeeded` only after `:111` and commit separately; write the `failed` status in that same out-of-band session so it survives the caller's rollback.

#### D3 — Davi INSERTs into Spring-owned `lifestyle_log` without maintaining the rollups Spring's charts read (medium)

`app/coredata/service.py:590`

**Mechanism.** `add_lifestyle_log` builds a `LifestyleLog` and `db.add`s it. mhn-spring keeps three pre-aggregated tables whose own schema comment says they are "maintained incrementally by the write path… since anything that writes the log directly would otherwise drift these silently". Davi is that thing and applies no delta. Separately, V14 added `drink_id`, `caffeine_mg`, `alcohol_units` and `source varchar(16) NOT NULL DEFAULT 'manual'`; `app/models/coredata.py:187-207` maps none of them.

**Consequence.** The rollup drift is bounded and self-healing: `_day_offset` (`app/chat/abilities.py:239`) allows only 0/1/2 days, inside Spring's 3-day reconcile window. The unbounded part is `caffeine_mg`/`alcohol_units` — per-row snapshots computed from `drink_id` at write time that the reconciler does not rebuild, so a chat-logged coffee contributes 0 mg forever. Provenance is recoverable via `metadata_json={"source":"davi_chat"}`, but the first-class `source` column takes its `manual` default. Already documented in `whole-app-coverage.md:597-605` as "raise, do not fix alone" and in open-item **D4 §2**.

**Fix.** Ask the mhn-spring team for the write endpoint that applies the delta (D4). If the direct insert stays, at minimum map `drink_id` and set `source` to a distinct value.

#### D4 — 16 of Davi's 23 owned tables have no schema check anywhere (medium)

`tests/test_flyway_parity.py:51-61`

**Mechanism.** `FLYWAY_TABLES` maps 7 tables. The comment at `:42` defers V6's tables to the coexistence test — which cannot cover them, because `scripts/build_existing_schema.py:35` deliberately **excludes every `davi_` Flyway file** from the dump, and `tests/test_coexistence.py` then asserts only table *presence* for 9 names, never column shape. Worse, `test_no_davi_flyway_file_is_left_unchecked` whitelists `V6__davi_ai_tables.sql` **by filename**, so it guards file coverage and reports green while 16 tables inside that file are unchecked — despite a docstring saying it exists to stop exactly that. The whole suite builds schema from `Base.metadata.create_all` (`tests/conftest.py:45`), so an added column on any of those 16 passes everything and fails only in production.

**Consequence.** Nothing has drifted today (I re-ran the parity parser over all 23 tables against the Flyway files: zero diffs). This is a hole, not a wound — but the exposed tables include `conversation_messages`, `insight_artifacts` and `consent_ledger`. Related: **C12** (the coexistence check runs nowhere automatically) and **C13** (interleaved Flyway chains).

**Fix.** Add V6's tables to `FLYWAY_TABLES`. The parser and assertions already handle all 16 unmodified; it is a dictionary edit and needs no Postgres.

#### D5 — Nothing verifies the Alembic chain against the models (medium)

`tests/test_migrations.py:31`

**Mechanism.** The reversibility test asserts three table *names* exist after upgrade, vanish after downgrade and return — nothing about columns, nothing compared to `Base.metadata`. `env.py` sets `target_metadata` and `compare_type=True`, but that only serves a human running autogenerate; no test asserts an empty diff. Both test engines build from `create_all`, and `pyproject.toml:74` sets `addopts = "-m 'not pg'"`, so the chain has zero coverage in the default suite.

**Consequence.** `Dockerfile:27` runs `alembic upgrade head` under `RUN_MIGRATIONS_ON_START=true`, documented in `railway.toml:22-25` as the way to stand up a standalone environment. A missing revision surfaces there as a runtime `UndefinedColumn` on the first request that touches it, with no test having failed. Blast radius is dev/standalone, since Flyway owns production.

**Fix.** One pg-marked test asserting `alembic.autogenerate.compare_metadata` is empty after `upgrade head`. Ten lines in the test that already builds the schema.

### Correctness — the tail

| Finding | Where | Mechanism → consequence | Fix |
|---|---|---|---|
| Retention never deletes `conversation_sessions` (low) | `app/chat/retention.py:115` | `purge_expired` covers messages, receipts and summaries; the cascade *parent* is deleted only by `purge_user`. After 180 days a session row survives with every child gone. `GET /chat/sessions` renders it as an empty entry (outer join + coalesce, so nothing breaks) that sorts below every live session. `ensure_session` mints a fresh session per turn when the client sends no id, so growth is per-turn, not per-conversation. No PHI, so not a compliance gap. | Purge sessions with no surviving messages older than the message window, in the same batched loop. |
| Every Flyway `CREATE` is `IF NOT EXISTS`, so applying to an Alembic-touched database silently no-ops (low) | `tests/test_flyway_parity.py:147` | PostgreSQL matches on name only, not shape. Requires pointing `RUN_MIGRATIONS_ON_START` at the shared DB, which `.env.example:44`, `Dockerfile:21-26` and `railway.toml:16-29` all forbid, and V21 already reconciles the cases that matter via catalog-guarded `DO $$` blocks and `ADD COLUMN IF NOT EXISTS`. Note the naive fix is worse: a bare `CREATE` aborts mhn-spring's whole chain. | Not the `IF NOT EXISTS`; add a post-migration `information_schema` shape assertion on shared environments. |
| The sweep rebuilds insights for users with a pending erasure, before executing it (low) | `scripts/nightly_sweep.py:53` | `recompute_insights` has no `is_pending` check. The memory-document half of this is **already fixed** — `app/memory/document.py:357-360` gates on `is_pending` with a test. And `recompute_insights` is hash-idempotent, the source rows deliberately survive the grace window, and `insight_artifacts` is in `_ERASE_IN_ORDER`, so for a due user the row written at `:53` is deleted at `:103` of the same run. Only a mid-window rules/template change writes anything. | Add the same `is_pending` skip for symmetry. Separately: `GET /insights` (`app/api/v1/insights.py:29`) has **no** `is_pending` check, so stored artifacts are served mid-grace — a real read-path hole. |
| `execute_due` caps at 500 per invocation with no queue-depth signal (low) | `app/chat/erasure.py:221` | One page, one call per sweep, no loop-until-drained, and the executed/failed counts are never persisted to the `job_runs` row. Reaching the cap needs 500+ users to have individually requested erasure and waited out 30 days; `POST /admin/sweep` can be re-invoked immediately; and `is_pending` already suppresses every memory read from the moment of the request, so a delayed purge is a bookkeeping lag, not continued use. | Return `pending_remaining` alongside the counts, or loop while the page is full. |
| `build_existing_schema` dies with a bare `IndexError` when the mhn-spring checkout is absent (low) | `scripts/build_existing_schema.py:47` | `Path.glob` on a missing directory returns empty; the header f-string then indexes `files[0]`. Reproduced. The command is documented in three places as the fix for the skipped coexistence check, so a teammate hits it at exactly the moment the sibling checkout is most likely missing, and the traceback never names the path it looked in. | `if not files: raise SystemExit(f"no V*__*.sql under {SPRING}; set MHN_SPRING_PATH")`. |
| `db/` is gitignored, so two schema guards run only on one machine (low) | `.gitignore:18` | `test_flyway_parity`, `test_coexistence` and the mhn-spring collision check all skip at module level on any other checkout, including CI. **Not** a single point of failure for the DDL: V6 and V21 are merged into mhn-spring's version-controlled chain, and gitignoring the copy was the deliberate fix for two copies drifting. `V23__davi_conversation_message_index.sql` has no adoption record, but its index is declared in tracked source at `app/models/chat.py:82`. Tracked as **C12**. | Regenerate the dump in CI with read access to mhn-spring, or accept and document that these two guards are local-only. |
| `/admin/registry/{code}/refresh` reports `index_reset: true` for a process-local cache (low) | `app/api/v1/admin.py:140` | The caches are module globals with a 300 s TTL. The deployed topology is single-process (`Dockerfile:27`, no `--workers`; one Railway service), so the claim is currently true; it becomes false the moment a second worker or replica appears, for ≤5 minutes, on non-safety-critical data. `app/rag/retrieval.py:516` already carries a `ponytail:` comment naming the tradeoff. | Rename the field to `index_reset_in_this_process`, or drop it. Note `drawbacks.md:235` (§4.6) is stale — it predates the TTL. |

---

## Reliability and operability

#### R1 — The production LLM call has no timeout: 600 s read × 3 attempts, per model call (high)

`app/llm/anthropic.py:284`

**Mechanism.** `AsyncAnthropic` is constructed with only `api_key`/`base_url`. Verified against the installed SDK (anthropic 1.0.0): `DEFAULT_TIMEOUT = Timeout(connect=5, read=600, write=600, pool=600)`, `DEFAULT_MAX_RETRIES = 2` — and httpx's `read` is per-read-op, so 1800 s is a floor. Legacy makes up to 2 calls per turn, agentic up to 5. Nothing wraps `handle_chat` in a request-level cap. **Every other outbound call in the repo is bounded** — translate 8 s, mhn-ai 10 s, mhn-spring 15 s, voice 30 s, openai_compat 60 s. The module docstring at `anthropic.py:2-4` ("The SDK owns retries, timeouts") is what makes it invisible; the SDK's default is a batch-job budget.

**Consequence.** This is the bug just fixed one directory over: `app/rag/embeddings.py:38-49` records a flat 180 s embedding timeout as the measured cause of the 43 s and 113 s staging turns. Not an outage — `ReleasingProvider` already frees the pooled DB connection before every model call, so a hung call is an idle coroutine, not pool exhaustion, and the client drops long before 30 minutes. The damage is tokens burned on retries the reader abandoned and a turn that fails slowly instead of fast.

**Fix.** `timeout=httpx.Timeout(read=20-30, connect=5)` and `max_retries=1` on the client, matching `embed_query`; then wrap the whole turn in `asyncio.timeout(chat_turn_budget_seconds)` so the worst case is bounded regardless of path.

#### R2 — Fail-open handlers swallow database errors without rolling back (high)

`app/chat/orchestrator.py:200-233` (`_write_receipt`, 15 call sites), `:611-616`, `:941-951`

**Mechanism.** `_write_receipt` does `db.add(...)` then `await db.flush()` inside a try that logs and returns — no `db.rollback()`, no enclosing SAVEPOINT. SQLAlchemy deactivates the transaction on a failed flush **on every backend**, sqlite included (reproduced: `PendingRollbackError` on the endpoint's later `db.commit()`). No fail-open handler in `app/` calls `rollback()` — the only one in the tree is in `erasure.py`. `record_fail_open` is an in-memory counter; nothing else recovers the session. The read-only sites (`open_episodes`, `build_health_snapshot`) are PG-specific: a failed SELECT aborts the transaction there but not on sqlite, so those are unreachable in the default suite. The tool registry gets this right (`app/chat/tools/registry.py:96` wraps each executor in `begin_nested`); the orchestrator's own guards do not.

**Consequence.** The docstring at `orchestrator.py:5-7` promises "a guardrail must never be a new way to break a reply" and `:231` promises "receipts must never break a reply". Both are false: one swallowed DB error converts a fully computed, successful, already-billed turn into a 500, after `record_fail_open` has logged a component that is not the cause.

**Fix.** Wrap each swallowing guard in `async with db.begin_nested():` (the pattern is already used at `:483` and `:495`), or `await db.rollback()` inside the except. Add one pg-marked test that fails a receipt insert mid-turn and asserts a 200.

#### R3 — The sweep cannot prove it has ever run (medium)

`scripts/nightly_sweep.py:103`, `.github/workflows/nightly-sweep.yml:44-55`

**Mechanism.** `execute_due` and `purge_expired` each have exactly one production caller, `run_sweep`, reachable only via `python -m scripts.nightly_sweep` or `POST /api/v1/admin/sweep`. There is no Railway cron service (`railway.toml:4-7` describes one in a comment). The GitHub workflow checks for `DAVI_BASE_URL`/`DAVI_SERVICE_TOKEN`, sets `configured=false` when either is missing, skips the POST and goes **green** — and even when configured it asserts only HTTP 202, while `admin.py` fires the sweep in a background task, so a sweep that *fails* also reports green.

**Consequence.** An unconfigured scheduler is indistinguishable from a working one, for the two most consequential jobs in the system. (The stronger claim — "it has never run" — is not verifiable from a checkout: the workflow is on the default branch with a live cron, and secret presence is invisible. And two impacts often attributed here are wrong: `recompute_insights` runs on every pedigree write, and the erasure *promise* is honoured immediately by `is_pending` gating every memory read.)

**Fix.** Fail the workflow when unconfigured rather than skipping; alert when `max(job_runs.finished_at) where name='nightly_sweep'` is older than 36–48 h. Set the two repo secrets and run it once manually.

#### R4 — The sweep runs as an untracked `asyncio` task inside the web process, with no lock (medium)

`app/api/v1/admin.py:186`, `railway.toml:3-7`

**Mechanism.** `asyncio.create_task(_run())` with the reference discarded, on the API service's own event loop; there is one Railway service and no worker. No concurrency guard — the sibling route at `admin.py:85-89` takes a `pg_advisory_xact_lock` for a far smaller race, and the workflow's `concurrency` group protects nothing, because the job POSTs and exits in seconds while the sweep runs for hours, so two *consecutive scheduled* runs overlap without anyone touching a manual trigger.

**Consequence.** A redeploy mid-sweep kills it — and because of D2, leaves **no** `job_runs` row rather than a stuck `running` one. Every phase is derived from current state and commits as it goes, so no completed destructive work is lost and the next run picks up the backlog. The named retention race is benign (second DELETE gets rowcount 0); the genuinely racy path nobody has named is two concurrent `recompute_insights` transactions creating duplicate artifacts. Pool and event-loop contention with live chat traffic is real. Open-item **C4** covers the one-transaction/serial-loop cost.

**Fix.** `pg_try_advisory_lock` at the top of `run_sweep`, 409 if held; keep a module-level task reference and null it in a done-callback. Longer term this belongs in the second Railway service `railway.toml:5-7` already describes.

#### R5 — The medication-allergy check fails silently and the counter meant to catch it is dead (medium)

`app/coredata/service.py:1029-1031`, `app/chat/orchestrator.py:494-501`

**Mechanism.** `medication_allergies` catches internally, logs a warning and returns `[]`. The orchestrator's outer try increments `record_fail_open("allergy_lookup")` — but the inner catch swallows first, so on sqlite the outer handler is unreachable (verified: a swallowed statement error inside `begin_nested` exits cleanly). `allergy_warning([])` returns `""`, and `build_drug_reply` prepends the warning only `if allergy_warning`, so the reply is byte-identical to a clean record. The same dead-counter pattern sits at `app/memory/document.py:117-128`, and `app/chat/tools/executors.py:244-250` is `except Exception: pass` with no log at all.

**Consequence.** On a transient DB error the drug reply ships with the allergy line missing and no metric moves. Calibrated down from the original filing: it *does* log a warning with a traceback; the reply still carries `MEDICATION_NOTE` plus `recommended_action="discuss_with_prescriber"`, so nothing asserts safety; and on production PostgreSQL the aborted subtransaction likely *does* reach the outer handler, making the counter live there. Same fail-open shape as S3.

**Fix.** Let `medication_allergies` propagate and let each caller decide — that revives three dead counters at once. If the allergy list cannot be read, say so ("I couldn't check your allergy record just now") rather than omit the line.

#### R6 — `/health` is a constant, and it is the deploy healthcheck (medium)

`app/api/v1/health.py:13-15`, `railway.toml:11`

**Mechanism.** `return {"status": "ok"}` — no DB, no config read. `create_app()` has no lifespan hook, `get_engine()` is lazy, and `get_settings()` has **zero module-level callers**, so the `_no_open_door_outside_dev` validator whose docstring says "a misconfigured deploy must die at startup" first executes inside a request handler. Reproduced: with `APP_ENV=prod AUTH_ENABLED=false DATABASE_URL=127.0.0.1:1`, import succeeds, `/health` returns 200, and `/api/v1/chat` 500s.

**Consequence.** A deploy with a wrong `DATABASE_URL`, an unreachable Postgres or an un-migrated schema is promoted to serving traffic and every chat turn 500s while Railway reports healthy. `restartPolicyType="on_failure"` works fine for anything that *exits* (import error, failed `alembic upgrade head`) — it just cannot fire here, because the process never exits. A constant liveness probe is correct in itself; what is missing is the readiness split.

**Fix.** Call `get_settings()` once in `create_app()` so a bad config kills the boot (this alone fixes the auth/secret class). Add `GET /ready` doing `SELECT 1` plus an expected-table check and point `healthcheckPath` at it; keep `/health` as liveness.

#### R7 — No logging configuration: every INFO line is discarded, every WARNING loses its level and logger name (medium)

`Dockerfile:27`, no `dictConfig`/`basicConfig` anywhere in `app/`

**Mechanism.** Measured against the installed uvicorn: after `dictConfig(LOGGING_CONFIG)`, root has no handler and level WARNING, and `logging.getLogger('davi.config').isEnabledFor(INFO)` is `False`. All 13 `logger.info` calls are dropped — including `app/config.py:176`, whose comment says it exists "so a dev-auth deploy is visible in the very first log lines", and the `_stage` instrumentation at `orchestrator.py:182-196` added specifically to diagnose the 43 s/113 s staging turns. WARNINGs fall through to `logging.lastResort`.

**Consequence.** Smaller than it first reads: `lastResort` still appends `exc_text`, and 72 of the 96 warn/exception sites pass `exc_info=True`, so warnings arrive as a message plus a full traceback with file and line — and the message strings name their component ("grounding/validation failed; safe reply", "allergy lookup failed; continuing"). What is genuinely lost is every INFO line, the level/timestamp/logger prefix, and any hope of a per-stage latency trace in production.

**Fix.** One `basicConfig`/`dictConfig` in `create_app()`: root at INFO, `%(asctime)s %(levelname)s %(name)s %(message)s` or JSON, level from `LOG_LEVEL`.

#### R8 — Nothing scrapes `/metrics`, nothing alerts (medium)

`app/api/v1/health.py:18-35`; no prometheus/grafana/alert config anywhere

**Mechanism.** Grepping outside `.venv` for prometheus/grafana/alertmanager/sentry/opentelemetry returns only `app/telemetry.py`, `health.py` and their two tests. No scrape target, no dashboard, no alert rule, no remote write. Counters are in-process dicts. **Stronger than filed:** the degradation reason is not persisted anywhere either — `_write_receipt` has no `degraded` column, so `provenance["degraded"]` is returned to the caller and dropped. `implementation-plan.md:2712` gives Task 20 the acceptance criterion "a dashboard answers what fraction of replies degraded last week, and why"; no dashboard exists.

**Consequence.** `davi_degradations_total` — the series the code calls "THE number that says whether the system is quietly answering badly" — is fetchable on demand and aggregated by nobody. Every degraded path does log a WARNING, so the signal is weak rather than absent (the one silent path is the ability-handler validation degradation at `orchestrator.py:880-885`).

**Fix.** Cheapest thing that works: a Grafana Cloud free-tier agent or a Railway cron curling `/metrics` into a hosted Prometheus, plus two alerts — `rate(davi_degradations_total)/rate(davi_chat_turns_total)` above a few percent, and `davi_fail_open_total{component=~"provider|grounding|allergy_lookup"}` nonzero. Persist the degraded reason on the receipt for retrospective queries. Until something reads it, write `/metrics` up as unmonitored.

#### R9 — `davi_llm_tokens_total` is always zero on the default engine, and cache tokens are dropped on both (medium)

`app/chat/orchestrator.py:1452-1455`, `:1765`; `app/llm/anthropic.py:246-252`

**Mechanism.** The counter's single increment site reads `result.provenance["usage"]`, populated only by `_dispatch_agentic`. The legacy path calls `provider.generate()`, declared `-> str`, which throws away `LLMTurn.usage`. `chat_engine` defaults to `legacy` and nothing overrides it outside CI and tests. Separately, the adapter deliberately surfaces `cache_creation_input_tokens`/`cache_read_input_tokens` ("a cache breakpoint that silently fails to cache looks EXACTLY like one that works"), `agent.py:65-70` sums them, and `:1453` then loops over only `("input_tokens","output_tokens")`. `agent.recover()`'s tokens are never accumulated on either engine.

**Consequence.** Tokens are spent and the counter reads 0, on the engine that answers real users, with no persisted alternative (`provenance` is never written to the DB). `/metrics` cannot be reconciled against the Anthropic bill, and the cache-hit rate that **A1** and **D1** both hinge on is measured only by `scripts/cache_probe.py` — `task-23-caching.md:147` records `cache_read_input_tokens > 0` as "NOT MEASURED".

**Fix.** Have the legacy path use `generate_turn` (or have `generate` return the turn) so usage survives; iterate every integer key in the usage dict rather than a hardcoded pair.

#### R10 — No rate limiting, no per-user budget, no idempotency, no middleware of any kind (medium)

`app/main.py:26-51`

**Mechanism.** `create_app()` registers nine routers and returns. No limiter, no request-id, no exception handler. Every authenticated `POST /chat` runs a billable model call; `PedigreePut.members` and `MemberIn.conditions` have per-field caps but no list-length cap. `ReleasingProvider` commits the reader's message before the first model call, so a client that times out and retries leaves two committed copies that both feed compaction and the recent-turns window.

**Consequence.** Tracked as `drawbacks.md:439` (§8.6) and `implementation-plan.md:2820` as an accepted risk delegated to the BFF's `withRateLimit()` — `docs/production_integration.md:138` confirms the production ingress is `chain(withRateLimit(), withAuth())`. Two claims commonly attached here are wrong: pool exhaustion cannot cascade to mhn-spring/mhn-ai (the 5+10 cap is a bulkhead, and `db_release.py` frees the connection across model calls), and per-turn work *is* bounded (4000-char message, 3 tool rounds, a 10 MB audio cap enforced pre-sidecar). What is genuinely undocumented is **idempotency** — 8.6 covers throttling only, and a rate limit would not stop a retry anyway.

**Fix.** An in-process token bucket keyed by user id (~20 lines, covers the retry-loop case) plus an optional client request id short-circuiting a repeat within the session; `max_length` on the two pedigree lists.

### Reliability — the tail

| Finding | Where | Mechanism → consequence | Fix |
|---|---|---|---|
| The retrieval/embedding fail-opens are entirely uncounted (low) | `app/rag/embeddings.py:80`, `app/rag/retrieval.py:585` | Zero `record_fail_open` calls in `app/rag`, despite the function's own docstring saying "call this from every except that continues". `provenance` records `used_rag: bool` and never which ranking mode ran. Mitigated more than filed: the failure that matters (sidecar timeout) **does** log a WARNING with `exc_info`, and `_stage("retrieval")` times it every legacy turn, so a 6 s timeout is distinguishable from a healthy hit. The three genuinely silent returns are static config facts (non-PG dialect, embeddings unconfigured, empty vector table). | Two `record_fail_open` calls plus a `"rank": "hybrid"|"keyword"` key in provenance. |
| A new Anthropic client and connection pool per request (low) | `app/api/v1/chat.py:60-62`, `app/llm/__init__.py:27-35` | `get_llm_provider` is a `Depends`, and `get_provider()` is not memoised, unlike `openai_compat.py:46`'s module-level `_shared_client`. FastAPI caches the dependency per request, so a whole agentic turn reuses one client — the cost is one extra TCP+TLS handshake per HTTP request, not five. Not a leak: the SDK's `AsyncHttpxClientWrapper.__del__` schedules `aclose()`. | `@lru_cache` on `get_provider()` — also fixes the per-call vision provider at `app/chat/tools/executors.py:320`. |
| Every turn re-reads the entire session transcript in `maybe_compact` (low) | `app/chat/conversation.py:105-118`, called from `orchestrator.py:1500` and `app/api/v1/chat.py:130` | `_ordered_messages` selects all messages for the session with no LIMIT and no column projection, full ORM rows including message text, on every turn, before the threshold early-return — and reads already-folded history too, since `covers_through_message_id` is resolved by `ids.index()` in Python. Sibling reads `_recent_messages` and `questions_asked` were both rewritten for exactly this reason, with a regression test that pins only `assemble_context`. Adds **zero** round trips, so it is not the cause of the staging curve (`test_turn_efficiency.py:196-203` records query count as flat 0→120 messages). Adjacent to **C5**. | `.where(ConversationMessage.id > covered_id)` plus a LIMIT; extend the budget test to assert every `conversation_messages` SELECT in a turn carries a bound. |
| The agentic engine has neither the stage timing nor the round-trip budget that found the last latency bug (low) | `app/chat/orchestrator.py:1528-1552`; `tests/test_turn_efficiency.py:204` | Legacy wraps six stages in `_stage`; `_dispatch_agentic` calls the same six bare, leaving one opaque band. The budget test reads `get_settings().chat_engine` at call time so it *would* run after a flip — but an unscripted `FakeProvider` emits no tool calls, so the agentic run does no tool round trips and the assertion passes vacuously. Whole-turn latency by engine survives via `chat_latency`. Belongs on **A2**'s prerequisite list beside **C8**. | Six `_stage(...)` wrappers; parametrise the budget test over both engines with scripted tool turns and its own ceiling. |
| Every dependency unpinned, no lock file, image never built in CI (low) | `pyproject.toml:6-19`, `Dockerfile:15`, `.github/workflows/ci.yml` | 13 `>=` constraints, `pip install .` resolving at build time, so two builds of one commit differ; the installed SDK is anthropic 1.0.0 against a floor of 0.40. Softer than filed: CI installs unpinned and imports `AsyncAnthropic` at module top with pyright over it, so the *unrecoverable* class (removed import surface) is caught; wire-shape drift behind an intact import fails open at `orchestrator.py:1141`/`:1602`/`streaming.py:118` to the safe reply, and a breaking framework major fails the healthcheck. Tracked as **B4** (adapter never smoke-tested against a real API). | Pin `anthropic>=1.0,<2.0` at minimum, generate a `requirements.lock` the Dockerfile installs, add `docker build .` to the quality job. |
| Single uvicorn process, no `--workers` (low) | `Dockerfile:27` | One core regardless of plan, on the service that also hosts the sweep. Per-turn CPU is milliseconds (BM25 is capped at 200 candidates by `GLOBAL_FALLBACK_CANDIDATES`; grounding is regex over one answer) against seconds of awaited I/O, so this is not the binding constraint at current concurrency. `--proxy-headers` is already uvicorn's default; the real (nil-consequence) gap is `forwarded_allow_ips`, and nothing in `app/` reads a client IP. | `--workers ${WEB_CONCURRENCY:-2}` when it is measured to matter. Note in-process metrics then split per worker, reinforcing R8. |
| **Documentation ledger is stale in the safe direction** (low) | `project_docs/open-items.md` C6 and C14; `README.md:128-138, 149, 24-26` | **C6/C14 are already fixed** — `app/chat/context.py:33-56` moved the memo into `db.info` under `_MEMO_KEY` with both failure modes documented as gone, pinned by `test_memo_does_not_survive_the_session`; the fix landed 2026-08-28 in `a656f46`, two days after open-items.md was last touched. `handover.md:4` points readers at that ledger, so its staleness costs re-investigation and calibration. Separately the README documents 5 of ~34 routes, says `AUTH_ENABLED` enables "HS256" when the default is HS512 with a Base64-decoded secret, names an `OllamaProvider` that no longer exists, and claims the default `alembic_version` table when it is `davi_alembic_version`. | Close C6 and C14 with a pointer to `context.py:43`; sweep the stale `_context_memo` reference at `app/chat/erasure.py:129`; regenerate the README route table from the router registrations and fix the three factual lines. |

---

## Missing features

### Promised but not wired

| Finding | Where | Mechanism → consequence | Fix |
|---|---|---|---|
| **M1** `/chat/stream` is not streaming (high) | `app/api/v1/chat.py:316-318, 362-366`; `app/chat/db_release.py:141` | `chat_stream` awaits `handle_chat` to completion, commits, and only then builds a generator that re-splits the finished string into sentences. Time-to-first-byte equals time-to-full-answer; no heartbeat. `generate_stream` is implemented on all three providers and correctly forwarded by `ReleasingProvider` — and has **zero call sites**. `validated_stream`'s incremental check therefore re-validates text `handle_chat` already validated, and no `final_check` is passed, so its `replace` escape hatch can never fire on unvalidated content. `implementation-plan.md:2455` specifies "time-to-first-token under 1 s on a live provider" and both the log and the plan mark Task 9 / drawback 3.1 done. `.github/PR_SCOPE_LATENCY.md:76-80` names switching mhn-react to this endpoint as the lever to get under 15 s — it would change nothing but the framing. | Wire `generate_stream` into the agentic engine's final answer round (which `anthropic.py:320-324` already anticipates) and feed the deltas to `validated_stream` with a `final_check`; or delete the endpoint and say `POST /chat` is the contract. Either way correct the two "done" marks. |
| **M2** Receipts are write-only and omit every field an incident needs (medium) | `app/models/chat.py:131-145`, `app/chat/orchestrator.py:200-231` | `RagTurnReceipt` stores `query_hash, model_name, prompt_version, retrieved, grounding, grounding_mode, grounding_status, used_rag` — no `risk_level`, no `recommended_action`, no `path`, no engine, no reply hash. An emergency receipt is indistinguishable from a greeting's. No code reads the table; `TurnFeedback.receipt_id`, the one join the design promises, is **never written and never read**. Retention windows are inverted for auditability (messages 180 d, receipts 400 d — see **C3**), so for 220 days a receipt points at destroyed content. Calibration: emergencies *are* countable via `davi_chat_turns_total{risk}`, enforce-mode replacements *are* findable via `grounding_mode='enforce' AND grounding_status='violations'`, and the reply text lives in `conversation_messages` with `extracted_intent` carrying the action for the message window. | Add `risk_level`, `path`, `engine`, `message_id`; either write `receipt_id` on feedback or drop the column; make receipt retention ≤ message retention or store enough to stand alone. |
| **M3** `VoiceSidecar.synthesize` is unreachable and the sidecar it needs has never been written (low) | `app/voice/service.py:164-184`; no voice directory | `synthesize` is complete and called by nothing; `ChatResponse` has no audio field. `translator/` ships as a deployable service with its own Dockerfile; there is no voice equivalent in any of the four repos, and `VOICE_BASE_URL` ships empty, so `/chat/voice` 503s on a default deploy. Honestly disclosed in three places and tracked as **B8** (whose "Needs" column reads "The ASR/TTS sidecar" — the artifact, not a deployment). 514 lines of test exercise the endpoint, ordering rule and confidence gate against a fake, so the Davi-side contract is real. | If voice is on the roadmap, build it the way `translator/` was built, and add `audio: str | None` to `ChatResponse` in the same change so `synthesize` stops being dead code. |
| **M4** No code path can grant clinician-reviewer standing (low) | `app/models/review.py`; no roster routes | `ClinicianReviewer` is constructed nowhere in `app/` or `scripts/`; `purge_user` can revoke a row but nothing can create one. **This is the designed grant mechanism**, stated in the model docstring, the Flyway DDL comment, and `decisions-needed.md` D9 ("a deliberate INSERT by an administrator… intentional for a first version"). What is genuinely missing is a bootstrap/seed path and an `insight_review_audit` row for the grant itself. | Optional: two service-token admin routes (grant/revoke) writing an audit row. At minimum, document the provisioning INSERT. |

### Designed but absent

#### M5 — The reader's own diagnoses, surgeries and non-medication allergies are never read (high)

`app/coredata/service.py:1002-1034` — the only `MedicalCondition` query in the codebase

**Mechanism.** `medical_condition` is mapped, registered in `COREDATA_TABLES`, and its model docstring says "conditions, surgeries AND allergies — one table split by `type`". The single query against it filters `type == 'allergy' AND category == 'medication'`. The `health_records(db, owner_id, kinds=('condition','surgery','allergy'), ...)` reader that `whole-app-coverage.md` §2.1 specifies as Stage 1 does not exist — grep finds no definition and no caller, and none of the 18 agentic tools reads medical history either. The memory document's `conditions` come from the self-typed, consent-gated Davi profile, not the app's medical history, while `render()` labels the whole block "the reader's own recorded data (cite as [P])". `lifestyle_limit` — the target half of every tracker answer — is unmapped, so `"Lifestyle entries: 4 water."` ships with nothing to compare against.

**Consequence.** Davi knows your grandfather had diabetes and does not know you have asthma. A beta-blocker question, an NSAID with recorded CKD, or a diet question with a recorded food allergy is answered blind, on data the shared database already holds and Spring already serves. `whole-app-coverage.md:69` calls this "the gap that matters most"; it is still the gap. Related: **D3** (the tracker write half) and **D4/D3** in open-items.

**Fix.** Build `health_records()` exactly as §2.1 specifies — the column allowlist, the `family_linked_relations` exclusion and the family-path predicate are all worked out there. Feed conditions and severe non-medication allergies into the memory document; fold `lifestyle_limit` into `build_health_snapshot`'s existing lifestyle line.

#### M6 — Hinglish and all romanized Indic is answered in English by default and on every sidecar failure (medium)

`app/i18n/language.py:50-66`, `app/translate/service.py:179-192`, `app/config.py:108`

**Mechanism.** `detect_language` is Unicode-script only and returns `en` for every Latin-script message, by design. Romanized-Indic identification exists *only* inside `pivot_inbound`'s Latin branch, which needs `translator.detect()` — and `translate_base_url` defaults to `""`, so `get_translator()` returns `None` and the branch never runs. Every failure mode also lands on English: sidecar down, non-200, 8 s timeout, confidence < 0.5, digit mismatch. `railway.toml` provisions only the api service; the translator's deployment exists as prose in `translator/README.md` (a ~3 GB image, one uvicorn worker, inference serialized behind a global lock).

**Consequence.** Comprehension and reply language degrade silently: English-only intent routing, ability parsers and drug/RAG keyword matching, and an English answer with **no notice** — `lang_hint` falls back to `detect_language`, which returns `en`, so the native-script "answering in English" notice never fires for romanized input. Importantly, **the safety floor is not affected**: `red_flags_i18n.py` carries 463 native-script *and* romanized phrases ("saans nahi aa rahi", "behosh", "daura pada") matched against the raw message with no sidecar. So this is a comprehension gap, not a silent safety hole.

**Fix.** Ship a `railway.translator.toml` (or refuse to start in prod with `TRANSLATE_BASE_URL` unset); alert on `davi_fail_open_total{component="translator"}`; add a ~15-line romanized-Hindi stopword backstop in `lang_hint` so the English-fallback notice at least fires when the sidecar is gone.

| Finding | Where | Mechanism → consequence | Fix |
|---|---|---|---|
| **M7** The insights engine covers three conditions, six rules and six relatives — no siblings (low) | `app/models/core.py:23-31`, `app/insights/core.py:203-210` | `PEDIGREE_SLOTS` is two parents and four grandparents; `Slot` is a hard `Literal` in `app/api/v1/schemas.py:11-18`, so a sibling is a 422. No consanguinity or ethnicity field. Narrower than it reads: `condition_code` is free-form, so a BRCA or thalassaemia entry against an allowed slot *is* stored and echoed in the patient context, and the empty state is explicit and honest rather than broken-looking; new conditions that fit an existing pattern are one seeded `RiskRule` row, not code. Only genuinely new pattern shapes (sibling-based, consanguinity) need code, and the storage columns are already `String(32)`. | Slot tuple + `Literal` + predicates for siblings; seed rows for new conditions. Everything here is DRAFT pending clinician sign-off anyway. |

### Expected by a clinician or a regulator, and absent

| Finding | Where | Mechanism → consequence | Fix |
|---|---|---|---|
| **M8** No data access or export path — DPDP rights are implemented in one direction (low) | `app/api/v1/profile.py`; `app/chat/erasure.py:58-59` | Erasure is complete (11 tables, 30-day cancellable window, use stopped immediately). There is no single call that bundles what Davi holds, and no grievance/data-protection contact anywhere. Softer than filed: `GET /profile`, `/profile/memory` (including `prompt_block` verbatim), `/insights`, `/pedigree`, `/chat/sessions` and `/chat/sessions/{id}/messages` already cover most categories per-endpoint. Genuinely unreadable: `symptom_logs`/`active_symptom_states`, long-term `user_memories` (which **do** feed prompts and are absent from the memory document's `_gather`), `consent_ledger`, receipts, feedback. A complete export would also need mhn-spring's data, so this is not Davi's obligation alone. | One read-only endpoint reusing `_ERASE_IN_ORDER` as its table list, serialised to JSON. Roughly the code of the erasure it mirrors. |
| **M9** No operator-facing quality or safety surveillance (low) | `app/api/v1/feedback.py:157`, `:226` | `GET /feedback/review` filters `user_id == current_user` and `POST /feedback/{id}/triage` calls `authorize_user` — so the only person who can mark a complaint handled is the complainant. The clinician queue in `review.py` is scoped to held *insight* artifacts and never touches chat. Calibration: the module's *endpoint* docstring states the scoping and says a cross-user view "belongs with the clinician review queue (Task 24), not bolted on here", `findings-phase-4.md:181` lists it under "considered and rejected", and `davi_degradations_total{engine,reason}` with a discriminating reason vocabulary already exposes the aggregate. The data is captured and joinable; only the endpoint is missing. The module *header* docstring ("a maintainer can see what readers actually disliked") oversells it. | A `SERVICE_TOKEN`- or roster-guarded cross-user feedback and violation queue; move `triage` to that authorization. Same shape as `_require_reviewer`. |
| **M10** No emergency number, no reminders/scheduling, and no statement of what Davi cannot do (low) | `app/triage/red_flags.py:309-312`; `app/chat/medication_flow.py:101-105`; `app/rag/prompt.py:37-47` | `EMERGENCY_DIRECTIVE` says "call your local emergency number" and names none, while `SELF_HARM_REPLY` names Tele-MANAS 14416 and explains why (the digit-fidelity check protects it through translation). Separately, "reminder"/"appointment"/"alarm"/"calendar" are in `_JUNK_WORDS` so the medication flow releases those turns; nothing else handles them, and the system prompt bars diagnosis but says nothing about **actions** — so "remind me at 9pm" reaches a model that has never been told it cannot set reminders, book appointments, or contact a doctor. No validator catches a false "I've set that for you". | One clinician-signed string change for 108/112; one paragraph in `_SAFETY_RULES` enumerating what Davi cannot do and pointing at the relevant app section. It sits inside the cached prefix, so it costs nothing per turn. |
| Body temperature has no reference range anywhere to grade against | — | Tracked as **C15**. Needs a clinician-approved band, ideally added to `traditional_health_parameters` so it arrives like every other range. Once it exists, wiring it is one entry in `_VALUE_METRIC_TERMS`. | — |

---

## What is genuinely good

Calibration, so the list above reads at its true weight. All of this was verified, not assumed.

- **The shared prologue really is shared.** Triage floor, scope guard, emergency directive, conversational replies, the interaction refusal, the dosing refusal and the drug-info reply all run above the engine fork (`orchestrator.py:563-790`), and the emergency return is unreachable from either engine's LLM path.
- **The translation pivot re-runs triage on the reader's original text and takes the max** (`orchestrator.py:576-590`) — the right direction. `digits_preserved` is order-sensitive and folds Indic digits.
- **The voice endpoint runs the floor on the transcript *before* the confidence gate** (`app/api/v1/chat.py:471-479`), with an excellent comment explaining why.
- **Object-level authorization is correct on all 25 routes walked** — chat, stream, voice, upload, sessions, messages, pedigree, insights, profile ×8, feedback ×4, review ×5, documents preview, admin ×2. No endpoint takes an owner id from a request body without `authorize_user`. `ensure_session` refuses a foreign session id rather than loading someone else's transcript into the prompt. Every agentic tool executor takes `user_id` from the server; `analyze_image` re-resolves the owner and runs the full consent gate on a model-supplied doc id.
- **SQL is entirely parameterised** — the only raw `text()` is a `pg_advisory_xact_lock` with a bound param. Metric labels come from bounded code-defined sets, capped at 200 series.
- **The document byte path** is consent-gated, streamed with a real (not post-buffer) size cap, content-type allowlisted, redirect-disabled, and audited with an actor id — with Spring's `assertCanRead` as the authoritative second check, deliberately.
- **Deferred erasure** — 11 tables, a cancellable window, and reads suppressed immediately by `is_pending` — is better than most products ship.
- **`ReleasingProvider`** (`app/chat/db_release.py`) exists specifically to free the pooled connection before every model call, with the concurrency arithmetic written out in its docstring. That is why R1 is a slow-failure problem and not an outage.
- **`has_escalation` and `discourages_care` are negation-aware**, which most implementations get wrong; `_normalize`/`_sentences` in `claims.py` correctly treat bullets and newlines as sentence boundaries; guard failures fail open to audited copy rather than 500ing (when they roll back — see R2).
- **The drug path refuses interactions and dosing rather than guessing**, and **A3**'s refuse-without-a-catalogue-match call is the right one.
- **`project_docs/` is an unusually honest record.** Several findings above are recorded there already, one (R10) at the correct severity with the correct mitigation, and `open-items.md` C11 escalates an audit-contract question rather than changing it unilaterally. The two stale entries in the "already fixed" direction are the exception, not the pattern.

---

## Recommended order of work

Ordering logic: **(1)** anything where a reader can be harmed by a single message goes first, regardless of effort; **(2)** then one-line fixes with outsized consequence, because they are free; **(3)** then the things that make the next audit unnecessary — observability and the checks that would have caught these; **(4)** then structural work; **(5)** documentation and scope last, because it costs the least when it waits.

1. **Triage recall for plain-language emergencies** (C1, C2, C6). The stroke/anaphylaxis/paediatric/obstetric gap, `discomfort` in `CHEST_STEM_RE`, and anchoring `"been cutting"`. First because it is the only finding where a single ordinary sentence produces a clinically wrong answer with no guard behind it, and because the symptom-combination tier generalises where a phrase list cannot. Needs clinician sign-off on the new phrases; the combination tier does not.
2. **Make the output guards do what the docs say they do** (C4 one-line reorder ×4, C3 three regex additions, C7 flip the default). Second because C4 is a reorder, C7 is a default, and together they turn three ornamental checks into real ones. Cheapest safety-per-line on the list.
3. **The one-line correctness fixes, batched** (D1 select-ids-first, D2 commit ordering, S10 one `delete()`, C-tail `risk_level=risk`, S3 narrowed except, R1 client timeout, R6 `get_settings()` in `create_app()`). Each is under ten lines, each closes a defect that fails silently, and batching them is one review instead of seven.
4. **The record-and-receipt helper, called from the voice path** (C5). Small, but it is the third instance of the bypass class and the fix (a helper both entry points call) is the structural answer, not a patch.
5. **Observability, before anything structural** (R7 one `dictConfig`, R8 a scraper plus two alerts, R9 usage on the legacy path, R3 fail the workflow when unconfigured plus a `job_runs` staleness alert). Fifth rather than later because everything below this line is work whose success you currently cannot observe — and because R3 is the difference between "erasure works" and "erasure has never been proven to run".
6. **The fail-open rollback sweep** (R2, R5, plus the uncounted `except`s). Mechanical but touches ~60 sites, so it wants its own change and its own pg-marked test. Doing it after step 5 means the revived counters have somewhere to be seen.
7. **The C8 handler audit and the eval-harness fix** (`run_evals` path wildcard, a self-harm scenario, a dosing scenario, a stroke scenario that fails today). This is the prerequisite for **A2**/Task 12 and the only thing that stops the bypass class recurring. Not higher because it is an audit, and audits are cheaper once the known instances are fixed.
8. **`health_records()` and the medication write-path checks** (M5, C10, C11). The largest product gap and the two clinical checks that pair with it. Below the guard work because it is net-new feature code needing Spring-side column decisions and clinician input on ranges, not a defect being closed.
9. **Schema guards** (D4 dictionary edit, D5 one autogenerate-diff test, then **C12**'s decision about where the coexistence check runs). Ninth because nothing has drifted yet — this buys the *next* six months, not this week.
10. **The sweep's home** (R4 advisory lock and task reference now; **C4**'s keyset pagination and a second Railway service when the sweep's wall clock crosses 30 minutes). Split deliberately: the lock is ten lines and prevents a real overlap; the move is infrastructure that can wait for the trigger the open item already names.
11. **`/chat/stream`, streaming for real** (M1) — or delete the endpoint. Either resolves a false "done" in the plan and stops a frontend change that cannot help.
12. **Security hardening with no live path** (S7, S8, S9, S12, S2's token split, S6's one-line gate). Last among code changes because each requires either a coordinated change with mhn-spring or an attacker who already holds something worse — but S6 and S12 are one line each and can ride along with any earlier batch.
13. **Documentation** (close C6/C14, correct the README's four wrong facts, correct `purge_user`'s docstring and `admin.py:163`'s polling claim, fold this document into the handover). Last because it costs the least when it waits — and first thing next session, because `handover.md` points the next reader at a ledger that is currently wrong about its own top items.
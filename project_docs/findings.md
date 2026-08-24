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

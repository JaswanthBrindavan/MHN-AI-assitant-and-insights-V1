# Decisions Needed

Choices made autonomously that you should review. Each one took the
**recommended** option and kept going, as instructed — nothing here is blocking.
If you disagree with any, say so and it will be changed.

Format: what was decided · why · what it looks like · how to reverse it.

---

## D1 — `utcnow()` is now strictly increasing (Task 1)

**Severity of the underlying bug: High. This one is worth reading first.**

### The problem

`app/models/common.py:utcnow()` returned `datetime.now(UTC)`. On this machine
**1000 consecutive calls returned one distinct value** — the system clock is
coarser than a burst of database inserts.

`conversation_messages` is ordered by `(created_at, id)` in six places. When
`created_at` ties, the tiebreak is `id` — a **random uuid4**. So message order
was random within any batch written in the same clock tick.

That is not cosmetic. `_ordered_messages` decides two things:

1. the recent turns the model is shown (`assemble_context`), and
2. which messages compaction folds (`maybe_compact`).

The observed failure was `covers_through_message_id` pointing at the wrong
message — **compaction folding the wrong turns**.

### What was decided

Make `utcnow()` strictly increasing per process: on a tie, bump one microsecond.

```python
# before — ties silently
def utcnow() -> datetime:
    return datetime.now(UTC)

# after — insertion order is recoverable from the timestamp alone
def utcnow() -> datetime:
    global _last_now
    with _clock_lock:
        now = datetime.now(UTC)
        if _last_now is not None and now <= _last_now:
            now = _last_now + timedelta(microseconds=1)
        _last_now = now
        return now
```

### Why this option

| Option | Verdict |
|---|---|
| **Monotonic `utcnow()`** ✅ | No schema change, fixes all six call sites at once, ~10 lines, fully testable |
| Add a sequence column | Correct, but **Flyway owns production schema** — needs a V11 migration coordinated with `mhn-spring`. Real cost, real lead time |
| Switch the PK to UUIDv7 | `uuid.uuid7()` is Python 3.14+; existing uuid4 rows would sort inconsistently against new ones |

### What you are trading

- **Every model's `created_at` is affected**, not just conversation messages.
  Timestamps become very slightly synthetic under burst writes.
- **Drift is bounded by write rate** — one microsecond per tied row. Reaching
  one second of drift needs a million writes in a tick.
- **Multi-process ties are still possible.** Two API workers writing in the same
  microsecond can still collide. The real case is covered: one turn's messages
  are written by one process in one transaction.

### If you disagree

Revert `app/models/common.py` and delete `tests/test_clock_monotonic.py`. Then
the correct fix is a monotonic sequence column on `conversation_messages`,
shipped as `db/flyway/V11__conversation_message_seq.sql` and adopted into
mhn-spring — larger, slower, and strictly more correct.

---

## D2 — Update call sites rather than a compat shim (Task 1)

`FakeProvider.calls` changed shape from `list[tuple]` to `list[dict]`. Options
were a shim preserving both, or editing the callers.

**Decided: edit the callers** (11 mechanical edits), per your "whichever is
better in the end". The shim would have cost more lines than the edits, and the
edits let **three duplicate outage stubs be deleted** — so the change is
net-negative in total lines. Reversing means re-adding a shim, which is strictly
more code.

---

## D3 — Task 12 cannot be completed autonomously 🔒

Task 12 deletes the regex handler chain (~1,200 lines). Its own gate, from the
plan:

1. `CHAT_ENGINE=agentic` passes run_evals — *achievable*
2. Task 21's quality suite scores agentic ≥ legacy — *achievable*
3. **one week running in staging with no regression** — *not achievable overnight*

Deleting the deterministic engine without condition 3 would be reckless: it is
the fallback that currently answers real users. **Task 12 is left undone and
the flag stays `CHAT_ENGINE=legacy` by default.** Everything else in Phase 1
ships behind that flag, so nothing changes for users until you flip it.

**Your call:** run agentic in staging for a week, then Task 12 is a small,
mechanical deletion.

---

## D4 — Tasks that need credentials or infrastructure this machine lacks

Built and tested as far as possible; the final step needs something not
available here.

| Task | Built | Cannot do | Why |
|---|---|---|---|
| 13 — provider bake-off | The harness, tested against fakes | Run it | Needs an Anthropic API key and/or a self-hosted model endpoint |
| 28 — Postgres in CI | The CI workflow + dual-backend fixture | Verify it | CLAUDE.md notes Docker is unavailable on the dev machine; `_hybrid_rank` short-circuits on non-Postgres |
| 2 — Anthropic adapter | Adapter + tests against a mocked SDK | Live smoke test | No API key configured |

None block later tasks. Each is one command away once the credential or
container exists.

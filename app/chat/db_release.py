"""Release the database connection before talking to the model.

`app/api/v1/chat.py` commits after `handle_chat` returns. Every write in a turn
therefore sits in one transaction that stays open across every provider call —
seconds of network wait with a pooled connection pinned to it and doing
nothing.

That is the binding constraint on concurrency long before storage or CPU is.
Derived in `project_docs/per-user-memory.md`: at a 3 s turn and a 4x diurnal
peak it needs ~167 concurrent connections at 1M users and ~1,667 at 10M,
against SQLAlchemy's default pool of 5 + 10 per process — on a database SHARED
with mhn-spring and mhn-ai, so exhausting it takes them down too. PgBouncer
cannot rescue this: in transaction mode it cannot release a transaction that
spans the model call.

The fix is a commit at the right moment, and the awkward part is *where*. The
provider is called from three modules that have no session: `run_agent` in
`app/chat/agent.py` (up to four calls per turn, interleaved with tool queries),
`_apply_grounding`'s corrective retry, and `maybe_compact`. Threading a session
into all of them would put database concerns inside a module whose docstring
says it owns control flow only.

So the session comes to the provider instead: a transparent wrapper that
commits immediately before each call. One mechanism, one place, no caller
changes.

**One call site it deliberately does NOT cover:** the `analyze_image` tool
(`app/chat/tools/executors.py`) fetches document bytes from S3 and runs a
vision model inside the SAVEPOINT that `app/chat/tools/registry.py` opens
around every tool. That is plausibly the longest single connection hold in the
system, and it is exactly the case this wrapper must not touch — committing
there would release the tool's savepoint and destroy its rollback-on-failure
isolation. Releasing it properly means giving that executor its own short-lived
session, which is a larger change than this one.

**This deliberately changes durability.** A turn is no longer one atomic
transaction: the reader's message is committed before the model is asked. That
is the better guarantee. A provider timeout or a crash mid-turn now leaves the
question on the record instead of discarding it, and tool writes the reader
asked for (a logged habit) survive a later failure in the same turn.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.tools import LLMTurn, Message, ToolSpec
from app.telemetry import db_commit_failures

logger = logging.getLogger("davi.chat")


class ReleasingProvider:
    """An LLM provider that commits the session before every model call.

    Transparent: it forwards `model_name` and anything else it does not define,
    so nothing downstream can tell it apart from the provider it wraps. That
    matters — the pipeline reads `provider.model_name` for receipts in eleven
    places, and tests pass spies that must still record what they are asked.
    """

    def __init__(self, provider, db: AsyncSession) -> None:
        self._provider = provider
        self._db = db
        # A plain attribute, not a property: LLMProvider declares model_name as
        # an attribute, and a property does not satisfy that for a type checker.
        self.model_name: str = provider.model_name

    # -- transparency -------------------------------------------------------
    def __getattr__(self, name: str):
        # Only reached for attributes this class does not define.
        return getattr(self._provider, name)

    # -- the point of the class --------------------------------------------
    async def _release(self) -> None:
        """Commit, so the connection goes back to the pool for the model call.

        **Never commits inside a SAVEPOINT.** `in_transaction()` is True for a
        nested transaction as well as a root one, and `Session.commit()`
        commits to the ROOT — "automatically releasing any SAVEPOINTs in
        effect". So the obvious guard does not protect a savepoint, it feeds
        one to a commit.

        That matters outside the request path. Four scripts (cost_report,
        live_analytics, stress_10k, stress_correlation) wrap `handle_chat` in
        `begin_nested()` + `rollback()` precisely so synthetic traffic leaves
        no trace. Releasing their savepoint would make ~9,500 fabricated
        chats PERMANENT in a database shared with mhn-spring and mhn-ai, and
        then raise `ResourceClosedError` on their rollback. Verified: after a
        commit inside a savepoint, `sp.is_active` is False and `sp.rollback()`
        raises.

        Skipping the release inside a savepoint fails SAFE: the connection
        stays pinned, which is merely the old behaviour, instead of destroying
        the caller's isolation boundary.

        On failure this raises. Note honestly what that means downstream: the
        call sites wrap provider calls in fail-open handlers, so a commit
        failure is reported to the reader as a degraded turn and to telemetry
        as `provider_error`. The dedicated counter below is what tells on-call
        the truth — that the DATABASE is down, not the model vendor.
        """
        if self._db.in_nested_transaction():
            # A savepoint is open; releasing it is the caller's decision, not
            # ours. Keep the connection rather than break their isolation.
            return
        if not self._db.in_transaction():
            return
        try:
            await self._db.commit()
        except Exception:
            # Increment BEFORE re-raising: the fail-open handler above will
            # relabel this as a provider error, and this counter is the only
            # place the real cause is visible.
            db_commit_failures.inc()
            logger.warning("commit before model call failed", exc_info=True)
            raise

    async def generate(self, *, system, user: str) -> str:
        await self._release()
        return await self._provider.generate(system=system, user=user)

    async def generate_turn(
        self,
        *,
        system,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> LLMTurn:
        await self._release()
        return await self._provider.generate_turn(
            system=system, messages=messages, tools=tools
        )

    async def generate_stream(
        self, *, system, messages: Sequence[Message]
    ) -> AsyncIterator[str]:
        """Unused today — /chat/stream chunks an already-finished reply.

        Kept because deleting it would be worse than useless: `__getattr__`
        would then forward `generate_stream` straight to the wrapped provider
        and silently skip the release. Note that as an async GENERATOR the
        release happens on first iteration, not at call time.
        """
        await self._release()
        async for chunk in self._provider.generate_stream(
            system=system, messages=messages
        ):
            yield chunk

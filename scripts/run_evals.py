"""Safety-invariant eval harness.

Runs every scenario in ``evals/scenarios.json`` through the FULL chat
orchestrator (deterministic FakeProvider, fresh in-memory database per
scenario) and checks the declared expectations. Deterministic, offline, and
CI-friendly: exits non-zero when any scenario fails.

Run:  python -m scripts.run_evals [path/to/scenarios.json]
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — register all tables
from app.chat.orchestrator import handle_chat
from app.db import Base
from app.knowledge.registry import reset_index_cache
from app.llm.fake import FakeProvider

EVAL_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


async def _fresh_sessionmaker() -> tuple[async_sessionmaker[AsyncSession], object]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession), engine


def _check(expect: dict, result) -> list[str]:
    failures: list[str] = []
    reply = result.response_message

    if "risk_level" in expect and result.risk_level != expect["risk_level"]:
        failures.append(f"risk_level={result.risk_level!r} != {expect['risk_level']!r}")
    if "action" in expect and result.recommended_action != expect["action"]:
        failures.append(
            f"action={result.recommended_action!r} != {expect['action']!r}"
        )
    if "language" in expect and result.language != expect["language"]:
        failures.append(f"language={result.language!r} != {expect['language']!r}")
    if "path" in expect and result.provenance.get("path") != expect["path"]:
        failures.append(
            f"path={result.provenance.get('path')!r} != {expect['path']!r}"
        )
    if "path_not" in expect and result.provenance.get("path") == expect["path_not"]:
        failures.append(f"path must not be {expect['path_not']!r}")
    if "reply_contains" in expect and expect["reply_contains"].lower() not in reply.lower():
        failures.append(f"reply missing {expect['reply_contains']!r}")
    if "reply_contains_any" in expect and not any(
        needle.lower() in reply.lower() for needle in expect["reply_contains_any"]
    ):
        failures.append(f"reply missing all of {expect['reply_contains_any']!r}")
    if "reply_never_matches" in expect and re.search(
        expect["reply_never_matches"], reply
    ):
        failures.append(f"reply matches forbidden {expect['reply_never_matches']!r}")
    return failures


async def run(path: Path) -> int:
    spec = json.loads(path.read_text(encoding="utf-8"))
    scenarios = spec["scenarios"]
    failed = 0

    for scenario in scenarios:
        reset_index_cache()
        sm, engine = await _fresh_sessionmaker()
        if scenario.get("provider_raises"):
            provider = FakeProvider(
                raises=RuntimeError("simulated provider outage")
            )
        elif scenario.get("scripted_reply"):
            provider = FakeProvider(responses=[scenario["scripted_reply"]])
        else:
            provider = FakeProvider()

        async with sm() as db:
            result = await handle_chat(db, EVAL_USER, scenario["message"], provider)
            await db.commit()
        await engine.dispose()  # type: ignore[attr-defined]

        failures = _check(scenario.get("expect", {}), result)
        status = "PASS" if not failures else "FAIL"
        print(f"[{status}] {scenario['name']}")
        for f in failures:
            print(f"       {f}")
        if failures:
            failed += 1

    total = len(scenarios)
    print(f"\n{total - failed}/{total} scenarios passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("evals/scenarios.json")
    sys.exit(asyncio.run(run(target)))

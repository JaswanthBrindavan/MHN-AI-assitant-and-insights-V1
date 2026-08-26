"""Admin registry refresh: alias card upsert + keyword-index cache reset."""

from __future__ import annotations

import pytest
from sqlalchemy import select

import app.knowledge.registry as registry
from app.config import get_settings
from app.models.chat import McpChunk
from app.models.knowledge import ConditionRegistry

TOKEN = "s" * 40
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _service_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "service_token", TOKEN)


async def _seed_condition(sessionmaker, *, active: bool = True, aliases=None):
    async with sessionmaker() as db:
        db.add(
            ConditionRegistry(
                condition_code="MC001",
                display_name="Type 2 Diabetes Mellitus",
                aliases=aliases if aliases is not None else ["test 1", "test 2", "test 3"],
                engine_codes=["T2DM"],
                active=active,
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_refresh_writes_alias_card_and_resets_cache(client, sessionmaker):
    await _seed_condition(sessionmaker)
    # Prime the cache so we can observe the reset.
    async with sessionmaker() as db:
        await registry.load_condition_index(db)
    assert registry._cache_loaded is True

    resp = await client.post("/api/v1/admin/registry/MC001/refresh", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["alias_card"] is True
    assert body["index_reset"] is True
    assert body["embedded"] is False  # no embedding service in tests — fail-open
    assert body["aliases"] == ["test 1", "test 2", "test 3"]
    assert registry._cache_loaded is False  # cache was reset

    async with sessionmaker() as db:
        chunks = (
            (await db.execute(select(McpChunk).where(McpChunk.condition_code == "MC001")))
            .scalars().all()
        )
    assert len(chunks) == 1
    assert chunks[0].chunk_type == "alias_card"
    assert "test 1" in chunks[0].content and "Type 2 Diabetes Mellitus" in chunks[0].content

    # Rebuilt index scopes the new alias to the condition.
    async with sessionmaker() as db:
        index = await registry.load_condition_index(db)
    assert index is not None
    assert "MC001" in index.match_message("what is test 1?")


@pytest.mark.asyncio
async def test_refresh_is_idempotent_single_card(client, sessionmaker):
    await _seed_condition(sessionmaker)
    for _ in range(2):
        resp = await client.post("/api/v1/admin/registry/MC001/refresh", headers=HEADERS)
        assert resp.status_code == 200
    async with sessionmaker() as db:
        chunks = (
            (await db.execute(select(McpChunk).where(McpChunk.condition_code == "MC001")))
            .scalars().all()
        )
    assert len(chunks) == 1  # replaced, not accumulated


@pytest.mark.asyncio
async def test_refresh_inactive_removes_card(client, sessionmaker):
    await _seed_condition(sessionmaker)
    await client.post("/api/v1/admin/registry/MC001/refresh", headers=HEADERS)
    async with sessionmaker() as db:
        row = (
            (
                await db.execute(
                    select(ConditionRegistry).where(
                        ConditionRegistry.condition_code == "MC001"
                    )
                )
            )
            .scalar_one()
        )
        row.active = False
        await db.commit()

    resp = await client.post("/api/v1/admin/registry/MC001/refresh", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["alias_card"] is False
    async with sessionmaker() as db:
        chunks = (
            (await db.execute(select(McpChunk).where(McpChunk.condition_code == "MC001")))
            .scalars().all()
        )
    assert chunks == []


@pytest.mark.asyncio
async def test_refresh_auth_required(client, sessionmaker):
    await _seed_condition(sessionmaker)
    assert (await client.post("/api/v1/admin/registry/MC001/refresh")).status_code == 401
    bad = {"Authorization": "Bearer wrong-token-wrong-token-wrong-token"}
    resp = await client.post("/api/v1/admin/registry/MC001/refresh", headers=bad)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_unknown_code_404(client):
    resp = await client.post("/api/v1/admin/registry/NOPE/refresh", headers=HEADERS)
    assert resp.status_code == 404

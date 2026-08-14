"""Phase 0 smoke test."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_versioned(client):
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200

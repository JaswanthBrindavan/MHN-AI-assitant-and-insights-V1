"""The streaming endpoint must be as safe as the buffered one."""

from __future__ import annotations

import json

import pytest

from app.api.v1.chat import get_llm_provider
from app.llm.fake import FakeProvider
from app.main import create_app

HDR = {"X-User-Id": "33333333-3333-3333-3333-333333333333"}


def _parse(body: str) -> list[dict]:
    events = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


def _shown(events: list[dict]) -> str:
    out = ""
    for e in events:
        if e["type"] == "delta":
            out += e["text"]
        elif e["type"] == "replace":
            out = e["text"]
    return out


@pytest.fixture
async def stream_client(sessionmaker):
    from httpx import ASGITransport, AsyncClient

    from app.db import get_db

    app = create_app()

    async def _override_db():
        async with sessionmaker() as session:
            yield session

    provider = FakeProvider(responses=["Sleep matters a lot. Try a routine."])
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_llm_provider] = lambda: provider

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, provider


async def test_a_normal_reply_streams_then_finishes(stream_client):
    client, _ = stream_client
    resp = await client.post(
        "/api/v1/chat/stream", headers=HDR, json={"message": "how does sleep work?"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse(resp.text)
    assert events[-1]["type"] == "done"
    assert any(e["type"] == "delta" for e in events)
    assert "Sleep matters" in _shown(events)


async def test_the_done_event_carries_the_same_metadata_as_the_buffered_route(
    stream_client,
):
    client, _ = stream_client
    resp = await client.post(
        "/api/v1/chat/stream", headers=HDR, json={"message": "how does sleep work?"}
    )
    done = _parse(resp.text)[-1]
    for field in (
        "risk_level",
        "recommended_action",
        "session_id",
        "provenance",
        "language",
        "trace",
    ):
        assert field in done, field


async def test_an_emergency_streams_the_deterministic_directive(stream_client):
    client, provider = stream_client
    resp = await client.post(
        "/api/v1/chat/stream", headers=HDR, json={"message": "I can't breathe"}
    )
    events = _parse(resp.text)
    done = events[-1]
    assert done["risk_level"] == "emergency"
    assert done["recommended_action"] == "call_emergency_services"
    assert "emergency" in _shown(events).lower()
    # The model is never consulted, streamed or not.
    assert provider.calls == []


async def test_a_banned_reply_never_reaches_the_client(sessionmaker):
    from httpx import ASGITransport, AsyncClient

    from app.db import get_db

    app = create_app()

    async def _override_db():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_llm_provider] = lambda: FakeProvider(
        responses=["You probably have diabetes."]
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/chat/stream",
            headers=HDR,
            json={"message": "tell me about blood sugar"},
        )

    assert "you probably have" not in resp.text.lower()


async def test_the_stream_requires_authorization(sessionmaker):
    from httpx import ASGITransport, AsyncClient

    from app.db import get_db

    app = create_app()

    async def _override_db():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_llm_provider] = lambda: FakeProvider()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/chat/stream",
            headers=HDR,
            json={
                "message": "hello",
                "user_id": "99999999-9999-9999-9999-999999999999",
            },
        )
    assert resp.status_code == 403

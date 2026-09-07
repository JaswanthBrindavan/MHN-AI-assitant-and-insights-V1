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


# --------------------------------------------------------------------------- #
# Real streaming: the first byte leaves before the answer is finished
# --------------------------------------------------------------------------- #
def _app_with(sessionmaker, provider):
    from app.db import get_db

    app = create_app()

    async def _override_db():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_llm_provider] = lambda: provider
    return app


async def asgi_post(app, path: str, body: dict, headers: dict, on_chunk=None):
    """POST straight into the ASGI app and time every body chunk.

    httpx's ASGITransport buffers the whole body before returning, which
    would hide exactly the thing under test. ``on_chunk`` sees each chunk
    as it is sent. Returns ``[(seconds since request, bytes), ...]``.
    """
    import asyncio
    import time

    raw = json.dumps(body).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (k.lower().encode(), v.encode())
            for k, v in {
                **headers,
                "content-type": "application/json",
                "content-length": str(len(raw)),
                "host": "test",
            }.items()
        ],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    delivered = False
    chunks: list[tuple[float, bytes]] = []
    started = time.perf_counter()

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.Event().wait()  # the client never disconnects

    async def send(message):
        if message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            chunks.append((time.perf_counter() - started, chunk))
            if on_chunk is not None:
                on_chunk(chunk)

    await app(scope, receive, send)
    return chunks


class _GatedProvider(FakeProvider):
    """Streams one sentence, then waits until the client has SEEN it."""

    def __init__(self) -> None:
        import asyncio

        super().__init__()
        self.gate = asyncio.Event()

    async def generate_stream(self, *, system, messages, tools=()):
        import asyncio

        from app.llm.tools import LLMTurn

        self.calls.append({"stream": True})
        yield "Sleep matters a lot. "
        # If nothing had reached the client yet this would time out, the
        # provider would fail, and the reader would get the safe reply —
        # which the assertions below would catch.
        await asyncio.wait_for(self.gate.wait(), 5)
        yield "Try a routine."
        yield LLMTurn(text="Sleep matters a lot. Try a routine.")


async def test_the_first_byte_arrives_before_the_answer_is_finished(sessionmaker):
    provider = _GatedProvider()
    app = _app_with(sessionmaker, provider)

    def _release_on_first_delta(chunk: bytes) -> None:
        if b'"type": "delta"' in chunk:
            provider.gate.set()

    chunks = await asgi_post(
        app, "/api/v1/chat/stream", {"message": "how does sleep work?"}, HDR,
        on_chunk=_release_on_first_delta,
    )
    events = _parse(b"".join(c for _, c in chunks).decode())
    assert events[0]["type"] == "delta"
    assert events[0]["text"] == "Sleep matters a lot. "
    assert events[-1]["type"] == "done"
    assert _shown(events) == "Sleep matters a lot. Try a routine."
    assert "degraded" not in events[-1]["provenance"]


async def test_a_value_the_fidelity_guard_rejects_is_never_emitted(sessionmaker):
    """No records, no retrieval, and the model states a dose: the buffered
    path replaces the reply. The stream must not have shown the number."""
    provider = FakeProvider(
        responses=["Take 500 mg of paracetamol. Then rest well."]
    )
    app = _app_with(sessionmaker, provider)
    chunks = await asgi_post(
        app, "/api/v1/chat/stream", {"message": "how does sleep work?"}, HDR
    )
    body = b"".join(c for _, c in chunks).decode()
    assert "500" not in body
    events = _parse(body)
    assert events[-1]["type"] == "done"
    assert events[-1]["provenance"]["degraded"] == "ungrounded_value"
    assert _shown(events)  # the safe reply, whole


async def test_post_chat_is_unchanged_and_the_stream_ends_on_the_same_text(
    sessionmaker,
):
    from httpx import ASGITransport, AsyncClient

    script = "Sleep matters a lot. Try a routine."
    buffered = FakeProvider(responses=[script])
    async with AsyncClient(
        transport=ASGITransport(app=_app_with(sessionmaker, buffered)),
        base_url="http://test",
    ) as client:
        plain = await client.post(
            "/api/v1/chat", headers=HDR, json={"message": "how does sleep work?"}
        )
    assert plain.status_code == 200
    # The buffered endpoint still makes the buffered call.
    assert "stream" not in buffered.calls[0]

    streamed = FakeProvider(responses=[script])
    chunks = await asgi_post(
        _app_with(sessionmaker, streamed), "/api/v1/chat/stream",
        {"message": "how does sleep work?"}, HDR,
    )
    events = _parse(b"".join(c for _, c in chunks).decode())
    assert streamed.calls[0]["stream"] is True
    assert _shown(events) == plain.json()["response_message"]
    assert events[-1]["risk_level"] == plain.json()["risk_level"]


async def test_a_provider_outage_still_degrades_to_the_safe_reply(sessionmaker):
    provider = FakeProvider(raises=RuntimeError("provider down"))
    chunks = await asgi_post(
        _app_with(sessionmaker, provider), "/api/v1/chat/stream",
        {"message": "how does sleep work?"}, HDR,
    )
    events = _parse(b"".join(c for _, c in chunks).decode())
    assert events[-1]["type"] == "done"
    assert events[-1]["provenance"]["degraded"] == "provider_error"
    assert _shown(events)


async def test_a_high_risk_turn_leads_with_the_escalation_banner(sessionmaker):
    from app.chat.replies import HIGH_ESCALATION

    provider = FakeProvider(responses=["Rest and keep a note of when it happens."])
    chunks = await asgi_post(
        _app_with(sessionmaker, provider), "/api/v1/chat/stream",
        {"message": "I have severe confusion since this morning"}, HDR,
    )
    events = _parse(b"".join(c for _, c in chunks).decode())
    assert events[0]["type"] == "delta"
    assert events[0]["text"].startswith(HIGH_ESCALATION)
    assert events[-1]["risk_level"] == "high"


async def test_the_agentic_engine_streams_and_retracts_a_tool_preamble(
    sessionmaker, monkeypatch,
):
    from app.config import get_settings
    from app.llm.tools import LLMTurn, ToolCall

    monkeypatch.setenv("CHAT_ENGINE", "agentic")
    get_settings.cache_clear()

    provider = FakeProvider(turns=[
        LLMTurn(
            # Two sentences: only a COMPLETED sentence is ever shown, so a
            # one-sentence preamble with no trailing whitespace is simply
            # never released.
            text="Let me look at your records. One moment.",
            tool_calls=(ToolCall(id="c1", name="get_health_summary", arguments={}),),
            stop_reason="tool_use",
        ),
        LLMTurn(text="Sleep matters a lot. Try a routine."),
    ])
    chunks = await asgi_post(
        _app_with(sessionmaker, provider), "/api/v1/chat/stream",
        {"message": "how does sleep work?"}, HDR,
    )
    events = _parse(b"".join(c for _, c in chunks).decode())
    kinds = [e["type"] for e in events]
    assert kinds[0] == "delta" and "look at your records" in events[0]["text"]
    assert {"type": "replace", "text": "", "reason": "tool_round"} in events
    assert events[-1]["type"] == "done"
    assert events[-1]["provenance"]["path"] == "agentic"
    assert events[-1]["provenance"]["tools"] == ["get_health_summary"]
    assert _shown(events) == "Sleep matters a lot. Try a routine."

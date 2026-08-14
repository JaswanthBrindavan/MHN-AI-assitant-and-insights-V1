"""Phase 5 — /chat endpoint wiring and deterministic paths."""

from __future__ import annotations

import pytest

HDR = {"X-User-Id": "11111111-1111-1111-1111-111111111111"}


@pytest.mark.asyncio
async def test_chat_emergency_directive(client):
    resp = await client.post("/api/v1/chat", headers=HDR, json={"message": "I can't breathe"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["risk_level"] == "emergency"
    assert body["recommended_action"] == "call_emergency_services"
    assert "emergency" in body["response_message"].lower()


@pytest.mark.asyncio
async def test_chat_scope_decline(client):
    resp = await client.post(
        "/api/v1/chat", headers=HDR, json={"message": "write me a python function"}
    )
    body = resp.json()
    assert body["recommended_action"] == "out_of_scope"
    assert body["risk_level"] == "none"


@pytest.mark.asyncio
async def test_chat_identity(client):
    resp = await client.post("/api/v1/chat", headers=HDR, json={"message": "who are you?"})
    body = resp.json()
    assert body["provenance"]["path"] == "conversational"
    assert "davi" in body["response_message"].lower()


@pytest.mark.asyncio
async def test_chat_data_query_no_data(client):
    resp = await client.post(
        "/api/v1/chat", headers=HDR, json={"message": "what is my family risk?"}
    )
    body = resp.json()
    assert body["provenance"]["path"] == "data_query"


@pytest.mark.asyncio
async def test_chat_symptom_rag_default(client):
    resp = await client.post(
        "/api/v1/chat", headers=HDR, json={"message": "what helps blood pressure?"}
    )
    body = resp.json()
    assert body["risk_level"] == "none"
    assert body["provenance"]["path"] == "symptom_rag"
    # Default fake answer is clean and diagnosis-free.
    assert body["response_message"]


@pytest.mark.asyncio
async def test_chat_idor_403(client):
    resp = await client.post(
        "/api/v1/chat",
        headers=HDR,
        json={"message": "hello", "user_id": "99999999-9999-9999-9999-999999999999"},
    )
    assert resp.status_code == 403

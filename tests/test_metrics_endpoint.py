"""Metrics end to end.

The point of this file is the last test: a degraded reply must be COUNTABLE.
The fail-open design is right, but a system that answers badly at scale while
looking healthy is the failure mode observability exists to prevent.
"""

from __future__ import annotations

import uuid

import pytest

from app import telemetry
from app.llm.fake import FakeProvider


@pytest.fixture(autouse=True)
def _clean_metrics():
    telemetry.reset_all()
    yield
    telemetry.reset_all()


async def test_metrics_is_served_in_prometheus_format(client):
    resp = await client.get("/api/v1/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "# TYPE davi_degradations_total counter" in resp.text


async def test_metrics_is_also_served_unversioned_for_scrapers(client):
    """/health is exposed both ways for load balancers; scrapers get the same."""
    assert (await client.get("/metrics")).status_code == 200


async def test_a_normal_turn_is_counted(client):
    await client.post(
        "/api/v1/chat",
        headers={"X-User-Id": "33333333-3333-3333-3333-333333333333"},
        json={"message": "how does sleep work?"},
    )
    body = (await client.get("/api/v1/metrics")).text
    assert 'davi_chat_turns_total{engine="legacy",risk="none"} 1' in body
    assert "davi_chat_turn_seconds_count" in body


async def test_an_emergency_turn_is_counted_at_its_risk_level(client):
    await client.post(
        "/api/v1/chat",
        headers={"X-User-Id": "33333333-3333-3333-3333-333333333333"},
        json={"message": "I can't breathe"},
    )
    body = (await client.get("/api/v1/metrics")).text
    assert 'risk="emergency"' in body


async def test_a_degraded_reply_is_countable_by_reason(db_session):
    """THE metric. Without it, the six fail-open paths degrade silently."""
    from app.chat.orchestrator import handle_chat

    provider = FakeProvider(responses=["You probably have diabetes."])
    await handle_chat(
        db_session, uuid.uuid4(), "tell me about blood sugar", provider
    )

    reasons = {
        dict(key).get("reason")
        for key in telemetry.degradations.values
    }
    assert "validation" in reasons


async def test_a_provider_outage_is_countable(db_session):
    from app.chat.orchestrator import handle_chat

    provider = FakeProvider(raises=RuntimeError("provider down"))
    await handle_chat(db_session, uuid.uuid4(), "what helps blood pressure?", provider)

    reasons = {
        dict(key).get("reason") for key in telemetry.degradations.values
    }
    assert "provider_error" in reasons
    # And the swallowed exception itself is counted, not just its consequence.
    components = {
        dict(key).get("component") for key in telemetry.fail_opens.values
    }
    assert "provider" in components


async def test_no_user_identifier_ever_reaches_a_label(client):
    """A label is not a log line you can redact later, and cardinality is
    unbounded storage."""
    user_id = "33333333-3333-3333-3333-333333333333"
    await client.post(
        "/api/v1/chat",
        headers={"X-User-Id": user_id},
        json={"message": "my sugar was 117 this morning"},
    )
    body = (await client.get("/api/v1/metrics")).text
    assert user_id not in body
    assert "117" not in body
    assert "sugar" not in body

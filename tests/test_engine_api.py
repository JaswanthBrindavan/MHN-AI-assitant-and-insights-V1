"""Phase 3 — engine + API + seeds.

Covers the end-to-end PUT→GET flow, the golden artifact snapshot, recompute
idempotency, sensitive→held_for_review gating, and cross-user IDOR (403).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.insights.engine import recompute_insights
from app.models.rules import InsightArtifact, InsightTemplate, RiskRule
from scripts.seed_rules_templates import seed_rules_and_templates
from scripts.seed_synthetic import USER_A, USER_B, USER_C, seed_synthetic

GOLDEN = Path(__file__).parent / "golden" / "artifacts.json"
USER_HDR = "11111111-1111-1111-1111-111111111111"


# --------------------------------------------------------------------------- #
# End-to-end: PUT pedigree → GET insights returns a rendered artifact
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_put_pedigree_then_get_insights(client, db_session):
    await seed_rules_and_templates(db_session)
    await db_session.commit()

    headers = {"X-User-Id": USER_HDR}
    put = await client.put(
        "/api/v1/pedigree",
        headers=headers,
        json={
            "members": [
                {
                    "slot": "mother",
                    "vital_status": "alive",
                    "conditions": [
                        {
                            "condition_code": "T2DM",
                            "condition_display": "type 2 diabetes",
                            "onset_band": "55_59",
                            "certainty": "confirmed",
                            "provenance": "self_report",
                        }
                    ],
                }
            ]
        },
    )
    assert put.status_code == 200
    assert put.json()["insights_created"] == 1

    got = await client.get("/api/v1/insights", headers=headers)
    assert got.status_code == 200
    insights = got.json()
    assert len(insights) == 1
    art = insights[0]
    assert art["condition_code"] == "T2DM"
    assert art["tier"] == "worth_knowing"
    assert "mother — type 2 diabetes, onset 55-59" in art["body"]
    # Mandatory safety sections are present.
    assert "not a diagnosis" in art["body"].lower()
    assert "next" in art["body"].lower()


@pytest.mark.asyncio
async def test_get_pedigree_excludes_soft_deleted(client, db_session):
    await seed_rules_and_templates(db_session)
    await db_session.commit()
    headers = {"X-User-Id": USER_HDR}
    await client.put(
        "/api/v1/pedigree",
        headers=headers,
        json={
            "members": [
                {
                    "slot": "mother",
                    "conditions": [
                        {
                            "condition_code": "T2DM",
                            "condition_display": "type 2 diabetes",
                            "onset_band": "55_59",
                            "certainty": "confirmed",
                        }
                    ],
                }
            ]
        },
    )
    ped = (await client.get("/api/v1/pedigree", headers=headers)).json()
    assert len(ped["conditions"]) == 1
    cond_id = ped["conditions"][0]["id"]

    dele = await client.delete(f"/api/v1/pedigree/conditions/{cond_id}", headers=headers)
    assert dele.status_code == 200

    ped2 = (await client.get("/api/v1/pedigree", headers=headers)).json()
    assert ped2["conditions"] == []
    # The insight is retracted after the condition is removed.
    insights = (await client.get("/api/v1/insights", headers=headers)).json()
    assert insights == []


# --------------------------------------------------------------------------- #
# Golden snapshot of all seeded artifacts (byte-identical on rerun)
# --------------------------------------------------------------------------- #
async def _snapshot(db) -> list[dict]:
    rows = (
        await db.execute(
            select(InsightArtifact)
            .where(InsightArtifact.user_id.in_([USER_A, USER_B, USER_C]))
            .order_by(
                InsightArtifact.user_id,
                InsightArtifact.condition_code,
                InsightArtifact.content_hash,
            )
        )
    ).scalars().all()
    return [
        {
            "user_id": str(r.user_id),
            "condition_code": r.condition_code,
            "tier": r.tier,
            "status": r.status,
            "title": r.title,
            "body": r.body,
            "facts_used": r.facts_used,
            "fired_rules": r.fired_rules,
            "template_key": r.template_key,
            "template_version": r.template_version,
            "pipeline_version": r.pipeline_version,
            "content_hash": r.content_hash,
        }
        for r in rows
    ]


@pytest.mark.asyncio
async def test_golden_artifacts_snapshot(db_session):
    await seed_synthetic(db_session)
    await db_session.commit()

    actual = await _snapshot(db_session)
    actual_json = json.dumps(actual, sort_keys=True, indent=2, ensure_ascii=True)

    if os.environ.get("GOLDEN_UPDATE"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(actual_json + "\n")

    expected_json = GOLDEN.read_text().rstrip("\n")
    assert actual_json == expected_json


@pytest.mark.asyncio
async def test_recompute_idempotent_zero_new_rows(db_session):
    await seed_synthetic(db_session)
    await db_session.commit()

    before = (
        await db_session.execute(select(InsightArtifact.id))
    ).scalars().all()

    # A second recompute with identical inputs must create nothing.
    for uid in (USER_A, USER_B, USER_C):
        created = await recompute_insights(db_session, uid, reason="rerun")
        assert created == []
    await db_session.commit()

    after = (
        await db_session.execute(select(InsightArtifact.id))
    ).scalars().all()
    assert set(before) == set(after)


# --------------------------------------------------------------------------- #
# Sensitive rule → held_for_review (never auto-surfaced)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sensitive_rule_is_held_for_review(client, db_session):
    await seed_rules_and_templates(db_session)
    # A sensitive rule + its own template.
    db_session.add(
        InsightTemplate(
            template_key="tmpl_onc",
            version=1,
            locale="en-IN",
            title="A note on your family history of {condition}",
            body=(
                "Family history of {condition}: {evidence}. "
                "{not_a_diagnosis} {next_step}"
            ),
            status="draft",
        )
    )
    db_session.add(
        RiskRule(
            rule_key="R-ONC-01",
            pattern_key="parental_count",
            params={"min": 1},
            condition_code="ONC",
            tier="worth_discussing",
            modifier=0,
            template_key="tmpl_onc",
            sensitive=True,
            active=True,
            version=1,
            rationale="DRAFT — sensitive oncology-adjacent family-history signal.",
        )
    )
    await db_session.commit()

    user_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
    headers = {"X-User-Id": str(user_id)}
    put = await client.put(
        "/api/v1/pedigree",
        headers=headers,
        json={
            "members": [
                {
                    "slot": "mother",
                    "conditions": [
                        {
                            "condition_code": "ONC",
                            "condition_display": "a cancer",
                            "onset_band": "50_54",
                            "certainty": "confirmed",
                        }
                    ],
                }
            ]
        },
    )
    assert put.status_code == 200

    # Persisted as held_for_review...
    art = (
        await db_session.execute(
            select(InsightArtifact).where(InsightArtifact.user_id == user_id)
        )
    ).scalars().first()
    assert art is not None
    assert art.status == "held_for_review"

    # ...and never surfaced by GET /insights.
    insights = (await client.get("/api/v1/insights", headers=headers)).json()
    assert insights == []


# --------------------------------------------------------------------------- #
# IDOR: acting on another user's data is a 403
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_idor_cross_user_403(client):
    me = "11111111-1111-1111-1111-111111111111"
    other = "99999999-9999-9999-9999-999999999999"
    resp = await client.get(
        f"/api/v1/insights?user_id={other}", headers={"X-User-Id": me}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_idor_put_cross_user_403(client):
    me = "11111111-1111-1111-1111-111111111111"
    other = "99999999-9999-9999-9999-999999999999"
    resp = await client.put(
        "/api/v1/pedigree",
        headers={"X-User-Id": me},
        json={"user_id": other, "members": []},
    )
    assert resp.status_code == 403

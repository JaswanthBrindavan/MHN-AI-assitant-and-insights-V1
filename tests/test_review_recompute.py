"""What a clinician's decision means when the engine runs again.

The review queue would be useless if the nightly sweep undid every decision.
These tests drive the REAL engine rather than asserting a constant, because
the property that matters is behavioural: a decision must survive a recompute
of the same facts, and must NOT survive a change to them.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.insights.engine import recompute_insights
from app.models.core import PedigreeCondition
from app.models.rules import InsightArtifact, InsightTemplate, RiskRule
from scripts.seed_rules_templates import seed_rules_and_templates

USER = uuid.UUID("55555555-5555-5555-5555-555555555555")


async def _seed_sensitive_rule(db) -> None:
    await seed_rules_and_templates(db)
    db.add(
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
    db.add(
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
    await db.flush()


async def _add_condition(db, onset_band: str = "50_54", slot: str = "mother") -> None:
    db.add(
        PedigreeCondition(
            user_id=USER,
            slot=slot,
            condition_code="ONC",
            condition_display="a cancer",
            onset_band=onset_band,
            certainty="confirmed",
            provenance="self_report",
            soft_deleted=False,
        )
    )
    await db.flush()


async def _artifacts(db) -> list[InsightArtifact]:
    return list(
        (
            await db.execute(
                select(InsightArtifact)
                .where(InsightArtifact.user_id == USER)
                .order_by(InsightArtifact.created_at)
            )
        ).scalars().all()
    )


@pytest.mark.asyncio
async def test_a_suppressed_insight_is_not_re_queued_by_a_recompute(db_session):
    """The decision must STICK across the nightly sweep.

    Without "suppressed" in LIVE_STATUSES the hash-supersede check cannot see
    the suppressed row, so every sweep creates a fresh held_for_review
    duplicate and the reviewer declines the same insight forever.
    """
    await _seed_sensitive_rule(db_session)
    await _add_condition(db_session)
    await recompute_insights(db_session, USER, reason="initial")

    held = await _artifacts(db_session)
    assert len(held) == 1 and held[0].status == "held_for_review"

    # The clinician withholds it.
    held[0].status = "suppressed"
    await db_session.flush()

    # The sweep runs again over identical facts.
    await recompute_insights(db_session, USER, reason="nightly")

    after = await _artifacts(db_session)
    assert len(after) == 1, "the recompute re-queued an insight already declined"
    assert after[0].status == "suppressed"


@pytest.mark.asyncio
async def test_a_released_insight_stays_released_across_a_recompute(db_session):
    """A clinician's release must not be silently revoked by the sweep."""
    await _seed_sensitive_rule(db_session)
    await _add_condition(db_session)
    await recompute_insights(db_session, USER, reason="initial")

    artifacts = await _artifacts(db_session)
    artifacts[0].status = "active"
    await db_session.flush()

    await recompute_insights(db_session, USER, reason="nightly")

    after = await _artifacts(db_session)
    assert len(after) == 1
    assert after[0].status == "active"


@pytest.mark.asyncio
async def test_changed_facts_produce_a_fresh_artifact_for_review(db_session):
    """A DIFFERENT insight has not been reviewed, whatever was decided before.

    This is the other half of the contract, and the more important one: a
    suppression must not become a permanent gag on a condition whose evidence
    later changes.
    """
    await _seed_sensitive_rule(db_session)
    await _add_condition(db_session, onset_band="50_54")
    await recompute_insights(db_session, USER, reason="initial")

    first = await _artifacts(db_session)
    first[0].status = "suppressed"
    await db_session.flush()

    # A second affected parent — different facts, different insight.
    await _add_condition(db_session, onset_band="40_44", slot="father")
    await recompute_insights(db_session, USER, reason="pedigree_write")

    after = await _artifacts(db_session)
    assert len(after) == 2, "changed facts did not produce a new artifact"
    statuses = {a.status for a in after}
    assert "held_for_review" in statuses, "the new insight skipped review"
    # The old one is superseded by the new, not left dangling as suppressed.
    superseded = [a for a in after if a.status == "superseded"]
    assert len(superseded) == 1
    assert superseded[0].superseded_by is not None

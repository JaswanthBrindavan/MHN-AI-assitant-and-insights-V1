"""Recompute engine — the only DB-touching part of the insights pipeline.

Reads pedigree + rules + templates, runs the pure core, and persists
reproducible artifacts with hash-based supersession. Reads NEVER compute; this
runs synchronously after every pedigree write and in the nightly sweep.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.insights import core
from app.insights.constants import NEXT_STEP_TEXT, NOT_A_DIAGNOSIS_TEXT
from app.models.core import PedigreeCondition
from app.models.rules import InsightArtifact, InsightTemplate, RiskRule

# Artifact statuses that are "live" (not retired).
LIVE_STATUSES = ("active", "held_for_review")


async def _load_inputs(db: AsyncSession, user_id: uuid.UUID) -> list[core.ConditionInput]:
    rows = (
        await db.execute(
            select(PedigreeCondition).where(
                PedigreeCondition.user_id == user_id,
                PedigreeCondition.soft_deleted.is_(False),
            )
        )
    ).scalars().all()
    return [
        core.ConditionInput(
            slot=r.slot,
            condition_code=r.condition_code,
            condition_display=r.condition_display,
            onset_band=r.onset_band,
            certainty=r.certainty,
            provenance=r.provenance,
        )
        for r in rows
    ]


async def _load_rules(db: AsyncSession) -> list[core.Rule]:
    rows = (
        await db.execute(select(RiskRule).where(RiskRule.active.is_(True)))
    ).scalars().all()
    return [
        core.Rule(
            rule_key=r.rule_key,
            pattern_key=r.pattern_key,
            condition_code=r.condition_code,
            tier=r.tier,
            modifier=r.modifier,
            template_key=r.template_key,
            sensitive=r.sensitive,
            version=r.version,
            params=r.params or {},
        )
        for r in rows
    ]


async def _load_template(
    db: AsyncSession, template_key: str
) -> InsightTemplate | None:
    return (
        await db.execute(
            select(InsightTemplate)
            .where(InsightTemplate.template_key == template_key)
            .order_by(InsightTemplate.version.desc())
        )
    ).scalars().first()


async def _live_artifacts(
    db: AsyncSession, user_id: uuid.UUID
) -> list[InsightArtifact]:
    return list(
        (
            await db.execute(
                select(InsightArtifact)
                .where(
                    InsightArtifact.user_id == user_id,
                    InsightArtifact.status.in_(LIVE_STATUSES),
                )
                .order_by(InsightArtifact.created_at.desc())
            )
        ).scalars().all()
    )


async def recompute_insights(
    db: AsyncSession, user_id: uuid.UUID, reason: str
) -> list[InsightArtifact]:
    """Assemble → evaluate → render → persist with hash-based supersession.

    Idempotent: if the live artifact for a (user, condition) has the same
    content hash, it is left untouched. Conditions that no longer produce an
    outcome have their live artifact retracted (superseded, no replacement).
    """
    settings = get_settings()

    inputs = await _load_inputs(db, user_id)
    facts = core.assemble_facts(inputs)
    rules = await _load_rules(db)
    outcomes = core.evaluate(facts, rules)

    live = await _live_artifacts(db, user_id)
    # Latest live artifact per condition (list is newest-first).
    latest_by_condition: dict[str, InsightArtifact] = {}
    for art in live:
        latest_by_condition.setdefault(art.condition_code, art)

    created: list[InsightArtifact] = []
    produced: set[str] = set()

    for outcome in outcomes:
        if not outcome.template_key:
            continue  # modifier-only outcome, nothing to render
        template = await _load_template(db, outcome.template_key)
        if template is None:
            continue  # cannot render without a template

        body = core.render_insight(
            template.body,
            condition=outcome.condition_display,
            facts=outcome.facts,
            not_a_diagnosis=NOT_A_DIAGNOSIS_TEXT,
            next_step=NEXT_STEP_TEXT,
        )
        facts_used = outcome.facts.to_dict()
        fired = list(outcome.fired_rule_keys)
        chash = core.content_hash(
            facts_used=facts_used,
            fired_rules=fired,
            tier=outcome.tier,
            template_key=template.template_key,
            template_version=template.version,
            body=body,
        )
        produced.add(outcome.condition_code)

        previous = latest_by_condition.get(outcome.condition_code)
        if previous is not None and previous.content_hash == chash:
            continue  # stable identity: no-op

        status = "held_for_review" if outcome.sensitive else "active"
        title = template.title.replace("{condition}", outcome.condition_display)
        artifact = InsightArtifact(
            user_id=user_id,
            condition_code=outcome.condition_code,
            tier=outcome.tier,
            title=title,
            body=body,
            facts_used=facts_used,
            fired_rules=fired,
            template_key=template.template_key,
            template_version=template.version,
            pipeline_version=settings.pipeline_version,
            content_hash=chash,
            status=status,
            recompute_reason=reason,
        )
        db.add(artifact)
        await db.flush()  # assign artifact.id
        if previous is not None:
            previous.status = "superseded"
            previous.superseded_by = artifact.id
        created.append(artifact)

    # Retract live artifacts for conditions that no longer produce an outcome.
    for condition_code, art in latest_by_condition.items():
        if condition_code not in produced:
            art.status = "superseded"
            art.recompute_reason = reason

    await db.flush()
    return created

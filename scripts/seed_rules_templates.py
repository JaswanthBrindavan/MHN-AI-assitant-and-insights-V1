"""Seed the DRAFT risk rules and insight templates.

DRAFT — pending clinician sign-off. Every rule's rationale cites its anchor.
Idempotent: upserts by (rule_key, version) and (template_key, version).

Run:  python -m scripts.seed_rules_templates
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.models.rules import InsightTemplate, RiskRule

# --- Rules (data; predicates live in app/insights/core.py) -------------------
RULES: list[dict] = [
    {
        "rule_key": "R-DM-01",
        "pattern_key": "parental_count",
        "params": {"min": 1},
        "condition_code": "T2DM",
        "tier": "worth_knowing",
        "modifier": 0,
        "template_key": "tmpl_dm",
        "sensitive": False,
        "version": 1,
        "rationale": "DRAFT — IDRS: a single affected parent raises family risk of T2DM.",
    },
    {
        "rule_key": "R-DM-02",
        "pattern_key": "both_parents",
        "params": {},
        "condition_code": "T2DM",
        "tier": "worth_discussing",
        "modifier": 0,
        "template_key": "tmpl_dm",
        "sensitive": False,
        "version": 1,
        "rationale": "DRAFT — IDRS: both parents affected is a stronger family-history signal.",
    },
    {
        "rule_key": "R-DM-03",
        "pattern_key": "early_onset_parent",
        "params": {"lt": 45},
        "condition_code": "T2DM",
        "tier": "worth_discussing",
        "modifier": 0,
        "template_key": "tmpl_dm",
        "sensitive": False,
        "version": 1,
        "rationale": "DRAFT — early parental onset (<45) is associated with higher familial risk.",
    },
    {
        "rule_key": "R-DM-04",
        "pattern_key": "vertical_transmission",
        "params": {},
        "condition_code": "T2DM",
        "tier": "typical",
        "modifier": 1,
        "template_key": None,
        "sensitive": False,
        "version": 1,
        "rationale": "DRAFT — a grandparent-parent vertical chain adds weight (modifier only).",
    },
    {
        "rule_key": "R-HTN-01",
        "pattern_key": "parental_count",
        "params": {"min": 1},
        "condition_code": "HTN",
        "tier": "worth_knowing",
        "modifier": 0,
        "template_key": "tmpl_htn",
        "sensitive": False,
        "version": 1,
        "rationale": "DRAFT — a single affected parent raises family risk of high blood pressure.",
    },
    {
        "rule_key": "R-CAD-01",
        "pattern_key": "premature_cad",
        "params": {"father_lt": 55, "mother_lt": 65},
        "condition_code": "CAD",
        "tier": "worth_discussing",
        "modifier": 0,
        "template_key": "tmpl_cad",
        "sensitive": False,
        "version": 1,
        "rationale": "DRAFT — premature CAD convention: father <55 / mother <65 at onset.",
    },
]

# --- Templates (must contain {condition}, {evidence}, {not_a_diagnosis},
#     {next_step}; the renderer refuses any template missing a safety section) -
_BODY = (
    "Some of your close family have {condition}. Here is what you shared: "
    "{evidence}.\n\n"
    "A family history like this can make {condition} somewhat more common within "
    "a family, but it is only one piece of the picture — everyday habits and many "
    "other factors matter too.\n\n"
    "{not_a_diagnosis}\n\n"
    "{next_step}"
)

TEMPLATES: list[dict] = [
    {
        "template_key": "tmpl_dm",
        "version": 1,
        "locale": "en-IN",
        "title": "A note on your family history of {condition}",
        "body": _BODY,
        "status": "draft",
    },
    {
        "template_key": "tmpl_htn",
        "version": 1,
        "locale": "en-IN",
        "title": "A note on your family history of {condition}",
        "body": _BODY,
        "status": "draft",
    },
    {
        "template_key": "tmpl_cad",
        "version": 1,
        "locale": "en-IN",
        "title": "A note on your family history of {condition}",
        "body": _BODY,
        "status": "draft",
    },
]


async def seed_rules_and_templates(db: AsyncSession) -> None:
    for spec in TEMPLATES:
        existing = (
            await db.execute(
                select(InsightTemplate).where(
                    InsightTemplate.template_key == spec["template_key"],
                    InsightTemplate.version == spec["version"],
                )
            )
        ).scalars().first()
        if existing is None:
            db.add(InsightTemplate(**spec))
        else:
            for k, v in spec.items():
                setattr(existing, k, v)

    for spec in RULES:
        existing = (
            await db.execute(
                select(RiskRule).where(
                    RiskRule.rule_key == spec["rule_key"],
                    RiskRule.version == spec["version"],
                )
            )
        ).scalars().first()
        if existing is None:
            db.add(RiskRule(active=True, **spec))
        else:
            for k, v in spec.items():
                setattr(existing, k, v)
    await db.flush()


async def _main() -> None:
    sm = get_sessionmaker()
    async with sm() as db:
        await seed_rules_and_templates(db)
        await db.commit()
    print(f"Seeded {len(RULES)} rules and {len(TEMPLATES)} templates (DRAFT).")


if __name__ == "__main__":
    asyncio.run(_main())

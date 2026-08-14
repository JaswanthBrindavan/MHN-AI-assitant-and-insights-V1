"""Deterministic insights core — pure, standard-library only.

This module contains no I/O, no ORM, no LLM, and no randomness. Given pedigree
condition inputs and a set of rules, it produces reproducible insight outcomes
and renders them into safe, plain-English text. Everything here is decision
support, never diagnosis.

Pipeline: assemble_facts → evaluate → render_insight, with content_hash for
reproducible artifact identity.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from app.insights.constants import (
    CERTAINTY_WEIGHTS,
    EVIDENCE_SLOT_ORDER,
    GRANDPARENT_SLOTS,
    GRANDPARENT_TO_PARENT,
    ONSET_MIDPOINTS,
    PARENT_SLOTS,
    SLOT_DISPLAY,
)

# Tier ordering (lowest → highest). "typical" means nothing notable.
TIERS: tuple[str, ...] = ("typical", "worth_knowing", "worth_discussing")
TIER_INDEX: dict[str, int] = {t: i for i, t in enumerate(TIERS)}


class TemplateContractError(Exception):
    """Raised when a template is missing a mandatory safety section."""


# --------------------------------------------------------------------------- #
# Inputs and assembled facts
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ConditionInput:
    """One pedigree condition row, reduced to what the engine needs."""

    slot: str
    condition_code: str
    condition_display: str
    onset_band: str
    certainty: str
    provenance: str


@dataclass(frozen=True)
class RelativeFact:
    slot: str
    onset_band: str
    onset_midpoint: int | None
    certainty: str
    provenance: str

    def to_dict(self) -> dict:
        return {
            "slot": self.slot,
            "onset_band": self.onset_band,
            "onset_midpoint": self.onset_midpoint,
            "certainty": self.certainty,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class ConditionFacts:
    condition_code: str
    condition_display: str
    parents: tuple[RelativeFact, ...]
    grandparents: tuple[RelativeFact, ...]
    min_parent_onset: int | None
    vertical_chain: bool
    weighted_load: float

    def parent_slots(self) -> set[str]:
        return {r.slot for r in self.parents}

    def grandparent_slots(self) -> set[str]:
        return {r.slot for r in self.grandparents}

    def to_dict(self) -> dict:
        return {
            "condition_code": self.condition_code,
            "condition_display": self.condition_display,
            "parents": [r.to_dict() for r in self.parents],
            "grandparents": [r.to_dict() for r in self.grandparents],
            "min_parent_onset": self.min_parent_onset,
            "vertical_chain": self.vertical_chain,
            "weighted_load": self.weighted_load,
        }


def _certainty_weight(certainty: str) -> float:
    return CERTAINTY_WEIGHTS.get(certainty, 0.0)


def _midpoint(onset_band: str) -> int | None:
    return ONSET_MIDPOINTS.get(onset_band)


def assemble_facts(inputs: list[ConditionInput]) -> dict[str, ConditionFacts]:
    """Group condition inputs into per-condition assembled facts."""
    by_condition: dict[str, list[ConditionInput]] = {}
    for row in inputs:
        by_condition.setdefault(row.condition_code, []).append(row)

    facts: dict[str, ConditionFacts] = {}
    for code, rows in by_condition.items():
        display = rows[0].condition_display

        parents: list[RelativeFact] = []
        grandparents: list[RelativeFact] = []
        for row in rows:
            rf = RelativeFact(
                slot=row.slot,
                onset_band=row.onset_band,
                onset_midpoint=_midpoint(row.onset_band),
                certainty=row.certainty,
                provenance=row.provenance,
            )
            if row.slot in PARENT_SLOTS:
                parents.append(rf)
            elif row.slot in GRANDPARENT_SLOTS:
                grandparents.append(rf)
            # Unknown slots are ignored defensively (should not occur).

        parent_slot_set = {r.slot for r in parents}

        parent_onsets = [r.onset_midpoint for r in parents if r.onset_midpoint is not None]
        min_parent_onset = min(parent_onsets) if parent_onsets else None

        vertical_chain = any(
            GRANDPARENT_TO_PARENT.get(gp.slot) in parent_slot_set for gp in grandparents
        )

        weighted_load = round(
            sum(_certainty_weight(r.certainty) for r in parents)
            + 0.5 * sum(_certainty_weight(r.certainty) for r in grandparents),
            4,
        )

        facts[code] = ConditionFacts(
            condition_code=code,
            condition_display=display,
            parents=tuple(parents),
            grandparents=tuple(grandparents),
            min_parent_onset=min_parent_onset,
            vertical_chain=vertical_chain,
            weighted_load=weighted_load,
        )
    return facts


# --------------------------------------------------------------------------- #
# Pattern registry — the only place predicates live (rules are data)
# --------------------------------------------------------------------------- #
def _p_parental_count(f: ConditionFacts, params: dict) -> bool:
    minimum = params.get("min", 1)
    return len(f.parent_slots()) >= minimum


def _p_both_parents(f: ConditionFacts, params: dict) -> bool:
    return set(PARENT_SLOTS) <= f.parent_slots()


def _p_grandparent_count(f: ConditionFacts, params: dict) -> bool:
    minimum = params.get("min", 1)
    return len(f.grandparent_slots()) >= minimum


def _p_early_onset_parent(f: ConditionFacts, params: dict) -> bool:
    lt = params.get("lt")
    if lt is None:
        return False
    for r in f.parents:
        if r.onset_midpoint is not None and r.onset_midpoint < lt:
            return True
    return False


def _p_vertical_transmission(f: ConditionFacts, params: dict) -> bool:
    return f.vertical_chain


def _p_premature_cad(f: ConditionFacts, params: dict) -> bool:
    father_lt = params.get("father_lt", 55)
    mother_lt = params.get("mother_lt", 65)
    for r in f.parents:
        if r.onset_midpoint is None:
            continue
        if r.slot == "father" and r.onset_midpoint < father_lt:
            return True
        if r.slot == "mother" and r.onset_midpoint < mother_lt:
            return True
    return False


PATTERNS = {
    "parental_count": _p_parental_count,
    "both_parents": _p_both_parents,
    "grandparent_count": _p_grandparent_count,
    "early_onset_parent": _p_early_onset_parent,
    "vertical_transmission": _p_vertical_transmission,
    "premature_cad": _p_premature_cad,
}


# --------------------------------------------------------------------------- #
# Rules and evaluation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rule:
    rule_key: str
    pattern_key: str
    condition_code: str
    tier: str
    modifier: int = 0
    template_key: str | None = None
    sensitive: bool = False
    version: int = 1
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    condition_code: str
    condition_display: str
    tier: str
    sensitive: bool
    fired_rule_keys: tuple[str, ...]
    template_key: str | None
    template_rule_key: str | None
    facts: ConditionFacts


def rule_fires(rule: Rule, facts: ConditionFacts) -> bool:
    """Evaluate a single rule's pattern against a condition's facts."""
    predicate = PATTERNS.get(rule.pattern_key)
    if predicate is None:
        raise KeyError(f"Unknown pattern_key: {rule.pattern_key}")
    return predicate(facts, rule.params or {})


def _resolve_template(fired: list[Rule]) -> tuple[str | None, str | None]:
    """Pick the template from the highest-tier fired rule that names one.

    Ties broken deterministically by rule_key. Returns (template_key, rule_key).
    """
    naming = [r for r in fired if r.template_key]
    if not naming:
        return None, None
    best = max(naming, key=lambda r: (TIER_INDEX[r.tier], _neg_key(r.rule_key)))
    return best.template_key, best.rule_key


def _neg_key(rule_key: str) -> tuple[int, ...]:
    """Invert a string for max()-with-tie-break so the lexically first wins."""
    return tuple(-ord(c) for c in rule_key)


def aggregate(fired: list[Rule], facts: ConditionFacts) -> Outcome:
    """Aggregate fired rules for one condition into a single outcome."""
    base_idx = max(TIER_INDEX[r.tier] for r in fired)
    net_modifier = sum(r.modifier for r in fired)
    idx = base_idx
    if net_modifier > 0:
        idx = min(idx + 1, len(TIERS) - 1)
    tier = TIERS[idx]

    sensitive = any(r.sensitive for r in fired)
    template_key, template_rule_key = _resolve_template(fired)
    fired_keys = tuple(sorted(r.rule_key for r in fired))

    return Outcome(
        condition_code=facts.condition_code,
        condition_display=facts.condition_display,
        tier=tier,
        sensitive=sensitive,
        fired_rule_keys=fired_keys,
        template_key=template_key,
        template_rule_key=template_rule_key,
        facts=facts,
    )


def evaluate(
    facts_by_condition: dict[str, ConditionFacts], rules: list[Rule]
) -> list[Outcome]:
    """Run every rule; return one outcome per condition with ≥1 fired rule."""
    rules_by_condition: dict[str, list[Rule]] = {}
    for rule in rules:
        rules_by_condition.setdefault(rule.condition_code, []).append(rule)

    outcomes: list[Outcome] = []
    for code in sorted(facts_by_condition):
        facts = facts_by_condition[code]
        fired = [r for r in rules_by_condition.get(code, []) if rule_fires(r, facts)]
        if not fired:
            continue
        outcomes.append(aggregate(fired, facts))
    return outcomes


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _onset_display(band: str) -> str:
    if band == "under_30":
        return "under 30"
    if band == "70_plus":
        return "70+"
    if band == "unknown":
        return "unknown"
    return band.replace("_", "-")


def build_evidence(facts: ConditionFacts) -> str:
    """Auto-build the evidence line, e.g. 'father — type 2 diabetes, onset 40-44'."""
    relatives = list(facts.parents) + list(facts.grandparents)
    order = {slot: i for i, slot in enumerate(EVIDENCE_SLOT_ORDER)}
    relatives.sort(key=lambda r: order.get(r.slot, len(EVIDENCE_SLOT_ORDER)))
    parts = [
        f"{SLOT_DISPLAY.get(r.slot, r.slot)} — {facts.condition_display}, "
        f"onset {_onset_display(r.onset_band)}"
        for r in relatives
    ]
    return "; ".join(parts)


def render_insight(
    template_body: str,
    *,
    condition: str,
    facts: ConditionFacts,
    not_a_diagnosis: str,
    next_step: str,
) -> str:
    """Render a template. Raises TemplateContractError if a mandatory section
    placeholder is missing."""
    if "{not_a_diagnosis}" not in template_body:
        raise TemplateContractError("template missing {not_a_diagnosis} section")
    if "{next_step}" not in template_body:
        raise TemplateContractError("template missing {next_step} section")

    return template_body.format(
        condition=condition,
        evidence=build_evidence(facts),
        not_a_diagnosis=not_a_diagnosis,
        next_step=next_step,
    )


# --------------------------------------------------------------------------- #
# Reproducible identity
# --------------------------------------------------------------------------- #
def content_hash(
    *,
    facts_used: dict,
    fired_rules: list,
    tier: str,
    template_key: str | None,
    template_version: int,
    body: str,
) -> str:
    """Stable sha256 over the artifact's meaning-bearing fields."""
    payload = json.dumps(
        {
            "facts_used": facts_used,
            "fired_rules": fired_rules,
            "tier": tier,
            "template": f"{template_key}:{template_version}",
            "body": body,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

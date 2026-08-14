"""Phase 2 — 100% branch coverage of the pure insights core.

Every pattern is exercised true AND false; certainty weighting, vertical-chain
slot mapping, tier aggregation with modifier, and the template contract are all
covered.
"""

from __future__ import annotations

import pytest

from app.insights.core import (
    ConditionFacts,
    ConditionInput,
    RelativeFact,
    Rule,
    TemplateContractError,
    aggregate,
    assemble_facts,
    build_evidence,
    content_hash,
    evaluate,
    render_insight,
    rule_fires,
)

DM = "T2DM"
DM_DISPLAY = "type 2 diabetes"


def ci(slot, onset="50_54", certainty="confirmed", provenance="self_report", code=DM,
       display=DM_DISPLAY):
    return ConditionInput(
        slot=slot,
        condition_code=code,
        condition_display=display,
        onset_band=onset,
        certainty=certainty,
        provenance=provenance,
    )


# --------------------------------------------------------------------------- #
# Fact assembly
# --------------------------------------------------------------------------- #
def test_assemble_parents_and_grandparents_split():
    facts = assemble_facts([ci("mother"), ci("grandmother_maternal")])[DM]
    assert facts.parent_slots() == {"mother"}
    assert facts.grandparent_slots() == {"grandmother_maternal"}


def test_assemble_unknown_slot_ignored():
    facts = assemble_facts([ci("mother"), ci("cousin")])[DM]
    assert facts.parent_slots() == {"mother"}
    assert facts.grandparent_slots() == set()


def test_min_parent_onset_present_and_absent():
    with_onset = assemble_facts([ci("mother", onset="40_44"), ci("father", onset="55_59")])[DM]
    assert with_onset.min_parent_onset == 42

    unknown_only = assemble_facts([ci("mother", onset="unknown")])[DM]
    assert unknown_only.min_parent_onset is None

    no_parents = assemble_facts([ci("grandmother_maternal")])[DM]
    assert no_parents.min_parent_onset is None


def test_vertical_chain_true_and_false():
    # Maternal grandmother + mother both affected → vertical chain.
    chain = assemble_facts([ci("mother"), ci("grandmother_maternal")])[DM]
    assert chain.vertical_chain is True

    # Paternal grandmother but no father → no chain (mapping matters).
    no_chain = assemble_facts([ci("mother"), ci("grandmother_paternal")])[DM]
    assert no_chain.vertical_chain is False


def test_vertical_chain_paternal_mapping():
    chain = assemble_facts([ci("father"), ci("grandfather_paternal")])[DM]
    assert chain.vertical_chain is True


def test_weighted_load_certainty_weighting():
    # 1.0 (mother confirmed) + 0.6 (father aya) + 0.5 * 1.0 (gp verified)
    facts = assemble_facts(
        [
            ci("mother", certainty="confirmed"),
            ci("father", certainty="as_far_as_i_know"),
            ci("grandmother_maternal", certainty="verified"),
        ]
    )[DM]
    assert facts.weighted_load == pytest.approx(2.1)


def test_facts_to_dict_roundtrip_keys():
    facts = assemble_facts([ci("mother", onset="40_44")])[DM]
    d = facts.to_dict()
    assert set(d) == {
        "condition_code",
        "condition_display",
        "parents",
        "grandparents",
        "min_parent_onset",
        "vertical_chain",
        "weighted_load",
    }
    assert d["parents"][0]["onset_midpoint"] == 42


# --------------------------------------------------------------------------- #
# Patterns — each true AND false
# --------------------------------------------------------------------------- #
def _facts(*inputs) -> ConditionFacts:
    return assemble_facts(list(inputs))[DM]


def test_parental_count():
    r = Rule("R", "parental_count", DM, "worth_knowing", params={"min": 1})
    assert rule_fires(r, _facts(ci("mother"))) is True
    assert rule_fires(r, _facts(ci("grandmother_maternal"))) is False


def test_parental_count_default_min():
    r = Rule("R", "parental_count", DM, "worth_knowing")  # params empty → min 1
    assert rule_fires(r, _facts(ci("mother"))) is True
    assert rule_fires(r, _facts(ci("grandmother_maternal"))) is False


def test_both_parents():
    r = Rule("R", "both_parents", DM, "worth_discussing")
    assert rule_fires(r, _facts(ci("mother"), ci("father"))) is True
    assert rule_fires(r, _facts(ci("mother"))) is False


def test_grandparent_count():
    r = Rule("R", "grandparent_count", DM, "worth_knowing", params={"min": 2})
    two = _facts(ci("grandmother_maternal"), ci("grandfather_paternal"))
    assert rule_fires(r, two) is True
    one = _facts(ci("grandmother_maternal"))
    assert rule_fires(r, one) is False


def test_grandparent_count_default_min():
    r = Rule("R", "grandparent_count", DM, "worth_knowing")
    assert rule_fires(r, _facts(ci("grandmother_maternal"))) is True
    assert rule_fires(r, _facts(ci("mother"))) is False


def test_early_onset_parent():
    r = Rule("R", "early_onset_parent", DM, "worth_discussing", params={"lt": 45})
    assert rule_fires(r, _facts(ci("mother", onset="40_44"))) is True
    assert rule_fires(r, _facts(ci("mother", onset="50_54"))) is False


def test_early_onset_parent_missing_lt_and_unknown_onset():
    no_lt = Rule("R", "early_onset_parent", DM, "worth_discussing")  # no lt param
    assert rule_fires(no_lt, _facts(ci("mother", onset="40_44"))) is False

    with_lt = Rule("R", "early_onset_parent", DM, "worth_discussing", params={"lt": 45})
    # Unknown onset is skipped (midpoint None).
    assert rule_fires(with_lt, _facts(ci("mother", onset="unknown"))) is False


def test_vertical_transmission():
    r = Rule("R", "vertical_transmission", DM, "typical", modifier=1)
    assert rule_fires(r, _facts(ci("mother"), ci("grandmother_maternal"))) is True
    assert rule_fires(r, _facts(ci("mother"))) is False


def test_premature_cad_father_and_mother_and_neither():
    r = Rule("R", "premature_cad", "CAD", "worth_discussing")
    # father onset 52 (< 55) → premature
    assert rule_fires(r, _facts(ci("father", onset="50_54"))) is True
    # mother onset 62 (< 65) → premature
    assert rule_fires(r, _facts(ci("mother", onset="60_64"))) is True
    # father onset 57 (>= 55) and mother onset 67 (>= 65) → not premature
    both_late = _facts(ci("father", onset="55_59"), ci("mother", onset="65_69"))
    assert rule_fires(r, both_late) is False


def test_premature_cad_unknown_onset_skipped():
    r = Rule("R", "premature_cad", "CAD", "worth_discussing")
    assert rule_fires(r, _facts(ci("father", onset="unknown"))) is False


def test_premature_cad_custom_thresholds():
    r = Rule("R", "premature_cad", "CAD", "worth_discussing",
             params={"father_lt": 60, "mother_lt": 70})
    assert rule_fires(r, _facts(ci("father", onset="55_59"))) is True


def test_rule_fires_unknown_pattern_raises():
    r = Rule("R", "no_such_pattern", DM, "typical")
    with pytest.raises(KeyError):
        rule_fires(r, _facts(ci("mother")))


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def test_aggregate_base_tier_is_max():
    facts = _facts(ci("mother"), ci("father"))
    fired = [
        Rule("R1", "parental_count", DM, "worth_knowing", template_key="t1"),
        Rule("R2", "both_parents", DM, "worth_discussing", template_key="t2"),
    ]
    out = aggregate(fired, facts)
    assert out.tier == "worth_discussing"
    assert out.template_key == "t2"  # highest tier names the template


def test_aggregate_modifier_steps_up():
    facts = _facts(ci("mother"), ci("grandmother_maternal"))
    fired = [
        Rule("R1", "parental_count", DM, "worth_knowing", template_key="t1"),
        Rule("R2", "vertical_transmission", DM, "typical", modifier=1),
    ]
    out = aggregate(fired, facts)
    # base worth_knowing + modifier step → worth_discussing
    assert out.tier == "worth_discussing"
    # template comes from the only template-naming rule (lower tier)
    assert out.template_key == "t1"
    assert out.template_rule_key == "R1"


def test_aggregate_modifier_capped_at_top():
    facts = _facts(ci("mother"), ci("father"))
    fired = [
        Rule("R1", "both_parents", DM, "worth_discussing", template_key="t"),
        Rule("R2", "vertical_transmission", DM, "typical", modifier=1),
    ]
    out = aggregate(fired, facts)
    assert out.tier == "worth_discussing"  # already top; cap holds


def test_aggregate_no_modifier_no_step():
    facts = _facts(ci("mother"))
    fired = [Rule("R1", "parental_count", DM, "worth_knowing", template_key="t")]
    out = aggregate(fired, facts)
    assert out.tier == "worth_knowing"


def test_aggregate_sensitive_flag():
    facts = _facts(ci("mother"))
    sensitive = [Rule("R1", "parental_count", DM, "worth_knowing",
                       template_key="t", sensitive=True)]
    assert aggregate(sensitive, facts).sensitive is True
    plain = [Rule("R1", "parental_count", DM, "worth_knowing", template_key="t")]
    assert aggregate(plain, facts).sensitive is False


def test_aggregate_no_template_when_none_named():
    facts = _facts(ci("mother"), ci("grandmother_maternal"))
    fired = [Rule("R2", "vertical_transmission", DM, "typical", modifier=1)]
    out = aggregate(fired, facts)
    assert out.template_key is None
    assert out.template_rule_key is None


def test_aggregate_template_tie_break_by_rule_key():
    facts = _facts(ci("mother"))
    fired = [
        Rule("R-B", "parental_count", DM, "worth_knowing", template_key="tb"),
        Rule("R-A", "parental_count", DM, "worth_knowing", template_key="ta"),
    ]
    out = aggregate(fired, facts)
    assert out.template_rule_key == "R-A"  # lexically first wins the tie


# --------------------------------------------------------------------------- #
# evaluate
# --------------------------------------------------------------------------- #
def test_evaluate_skips_conditions_with_no_fired_rules():
    facts = assemble_facts([ci("grandmother_maternal")])  # only a grandparent
    rules = [Rule("R1", "both_parents", DM, "worth_discussing", template_key="t")]
    assert evaluate(facts, rules) == []


def test_evaluate_returns_outcome_when_a_rule_fires():
    facts = assemble_facts([ci("mother"), ci("father")])
    rules = [Rule("R1", "both_parents", DM, "worth_discussing", template_key="t")]
    outcomes = evaluate(facts, rules)
    assert len(outcomes) == 1
    assert outcomes[0].condition_code == DM


def test_evaluate_condition_without_rules():
    facts = assemble_facts([ci("mother", code="RARE", display="rare thing")])
    # No rules reference RARE → get() default [] path, no outcome.
    assert evaluate(facts, []) == []


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
GOOD_TEMPLATE = (
    "About {condition}. Evidence: {evidence}. {not_a_diagnosis} {next_step}"
)


def test_render_ok():
    facts = _facts(ci("father", onset="40_44"), ci("mother", onset="50_54"))
    body = render_insight(
        GOOD_TEMPLATE,
        condition=DM_DISPLAY,
        facts=facts,
        not_a_diagnosis="ND",
        next_step="NS",
    )
    assert "father — type 2 diabetes, onset 40-44" in body
    assert "mother — type 2 diabetes, onset 50-54" in body
    assert "ND" in body and "NS" in body


def test_render_missing_not_a_diagnosis_raises():
    bad = "About {condition}. {next_step}"
    with pytest.raises(TemplateContractError):
        render_insight(bad, condition=DM_DISPLAY, facts=_facts(ci("mother")),
                       not_a_diagnosis="ND", next_step="NS")


def test_render_missing_next_step_raises():
    bad = "About {condition}. {not_a_diagnosis}"
    with pytest.raises(TemplateContractError):
        render_insight(bad, condition=DM_DISPLAY, facts=_facts(ci("mother")),
                       not_a_diagnosis="ND", next_step="NS")


def test_build_evidence_onset_display_variants():
    facts = _facts(
        ci("father", onset="under_30"),
        ci("mother", onset="70_plus"),
        ci("grandmother_maternal", onset="unknown"),
    )
    line = build_evidence(facts)
    assert "onset under 30" in line
    assert "onset 70+" in line
    assert "onset unknown" in line
    # Deterministic ordering: father before mother before grandparent.
    assert line.index("father") < line.index("mother") < line.index("grandmother")


def test_build_evidence_hand_built_unknown_slot_order_default():
    # Directly construct facts with a slot outside EVIDENCE_SLOT_ORDER.
    weird = ConditionFacts(
        condition_code=DM,
        condition_display=DM_DISPLAY,
        parents=(RelativeFact("mother", "40_44", 42, "confirmed", "self_report"),),
        grandparents=(RelativeFact("uncle", "50_54", 52, "confirmed", "self_report"),),
        min_parent_onset=42,
        vertical_chain=False,
        weighted_load=1.5,
    )
    line = build_evidence(weird)
    # mother (ordered) appears before the unknown-slot relative (default order).
    assert line.index("mother") < line.index("uncle")


# --------------------------------------------------------------------------- #
# content_hash
# --------------------------------------------------------------------------- #
def test_content_hash_stable_and_sensitive():
    h1 = content_hash(
        facts_used={"a": 1},
        fired_rules=["R1"],
        tier="worth_knowing",
        template_key="t",
        template_version=1,
        body="hello",
    )
    h2 = content_hash(
        facts_used={"a": 1},
        fired_rules=["R1"],
        tier="worth_knowing",
        template_key="t",
        template_version=1,
        body="hello",
    )
    assert h1 == h2
    assert len(h1) == 64

    changed = content_hash(
        facts_used={"a": 1},
        fired_rules=["R1"],
        tier="worth_knowing",
        template_key="t",
        template_version=1,
        body="different",
    )
    assert changed != h1


def test_content_hash_none_template():
    h = content_hash(
        facts_used={},
        fired_rules=[],
        tier="typical",
        template_key=None,
        template_version=1,
        body="x",
    )
    assert len(h) == 64

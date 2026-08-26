"""Matching a stated value to the backend's reference parameter.

`app/health/reference.py::_match_thp` picks a `traditional_health_parameters`
row by UNANCHORED SUBSTRING, from an unordered `SELECT`, first-match-wins. That
was harmless while the table was empty — `_match_thp` returned None and
`app/chat/data_handlers.py` fell back to Davi's own clinically correct
constants in `app/health/ranges.py`.

mhn-spring's `V18__thp_catalogue.sql` is the first migration in the chain that
POPULATES that table — 193 parameters, including several whose names contain
each other. And `data_handlers.py` prefers the backend band **unconditionally**
over Davi's constants.

So V18 does not degrade a working feature. It replaces three correct answers
with wrong ones, and nobody has to do anything for it to happen — Flyway
applies it.

The rows below are copied verbatim from V18 (line numbers in the comments), so
these tests describe production, not a hypothetical.
"""

from __future__ import annotations

import pytest

from app.health.reference import evaluate_backend
from app.models.coredata import ThpAgeRange, TraditionalHealthParameter


async def _seed_v18(db) -> None:
    """The colliding subset of V18's catalogue, in V18's own insert order.

    Order matters: `_match_thp` has no ORDER BY and takes the first row that
    matches at the best rank, so insertion order decides the winner.
    """
    rows = [
        # name, unit, status, (low_danger, low_warn, ideal, high_warn, high_danger)
        ("CHOL/HDL ratio", "Ratio", "approved", (0.0, 3.0, 4.0, 5.0, 8.4)),      # V18:54
        ("Glycated Hemoglobin (HbA1c)", "%", "approved", (0.0, 4.0, 5.0, 5.7, 16.1)),  # V18:79
        ("HDL/LDL Ratio", "ratio", "draft", (0.0, 0.4, 1.0, 999.0, 1198.8)),     # V18:84
        ("Hemoglobin", "g/dL", "approved", (8.0, 12.0, 14.0, 17.0, 22.0)),       # V18:86
        ("LDL Cholesterol", "mg/dL", "approved", (0.0, 50.0, 80.0, 100.0, 228.0)),  # V18:99
    ]
    for name, unit, _status, bands in rows:
        thp = TraditionalHealthParameter(name=name, units=unit, aliases=[])
        db.add(thp)
        await db.flush()
        low_danger, low_warn, ideal, high_warn, high_danger = bands
        db.add(
            ThpAgeRange(
                thp_id=thp.id,
                age_min=0,
                age_max=120,
                min=low_danger,
                low_danger=low_danger,
                low_warn=low_warn,
                ideal=ideal,
                high_warn=high_warn,
                high_danger=high_danger,
                max=high_danger,
            )
        )
    await db.flush()


@pytest.mark.parametrize(
    ("metric", "value", "must_not_be"),
    [
        # A statin-territory LDL must never be called normal. Pre-V18 Davi
        # said "above the typical range"; "HDL/LDL Ratio" spans 0.4–999, so
        # 190 lands mid-range.
        ("ldl", 190.0, "normal"),
        # An ordinary HDL must not route to urgent care. "CHOL/HDL ratio"
        # tops out at 8.4, so any real mg/dL reading is "danger".
        ("hdl", 45.0, "danger"),
    ],
)
async def test_a_value_is_not_graded_against_the_wrong_parameter(
    db_session, metric, value, must_not_be
):
    """The failure is SILENT: a confident, well-formed, wrong answer."""
    await _seed_v18(db_session)

    verdict = await evaluate_backend(db_session, metric, value, 40)

    # Two acceptable outcomes, and one unacceptable one. Either the backend
    # matched the RIGHT parameter, or it matched nothing and the caller falls
    # back to Davi's own constants. What must never happen is a confident
    # grading against the wrong parameter.
    if verdict is None:
        return
    assert verdict.severity != must_not_be, (
        f"{metric}={value} was graded {verdict.severity!r} against "
        f"{verdict.label!r} ({verdict.unit}) — the wrong parameter"
    )


async def test_ldl_matches_ldl_cholesterol_not_a_ratio(db_session):
    await _seed_v18(db_session)
    verdict = await evaluate_backend(db_session, "ldl", 190.0, 40)
    assert verdict is not None
    assert verdict.label == "LDL Cholesterol", (
        f"matched {verdict.label!r}"
    )
    assert verdict.unit == "mg/dL"


async def test_hdl_falls_back_rather_than_matching_a_ratio(db_session):
    """No "HDL Cholesterol" row exists in this subset.

    The honest outcome is NO match — fall back to Davi's own correct constants
    — rather than grading a mg/dL value against a dimensionless ratio. Falling
    back is always the safe direction: it is what happened before the
    catalogue existed, and those constants are right.
    """
    await _seed_v18(db_session)
    verdict = await evaluate_backend(db_session, "hdl", 45.0, 40)
    assert verdict is None, (
        f"an HDL in mg/dL was graded against {verdict.label!r}"
        if verdict else ""
    )


async def test_hemoglobin_is_not_graded_as_hba1c(db_session):
    """The direction inverts: anaemia is reported as HIGH, in percent."""
    await _seed_v18(db_session)

    verdict = await evaluate_backend(db_session, "hemoglobin", 8.0, 40)

    assert verdict is not None
    assert verdict.label == "Hemoglobin", (
        f"matched {verdict.label!r} — a low haemoglobin would be "
        f"reported as HIGH in %"
    )
    assert verdict.unit == "g/dL"
    assert verdict.direction != "high", "anaemia reported as high"


async def test_an_unmapped_metric_falls_back_rather_than_guessing(db_session):
    """No match must mean "use Davi's own constants", never "closest guess"."""
    await _seed_v18(db_session)
    assert await evaluate_backend(db_session, "spo2", 97.0, 40) is None


async def test_matching_is_deterministic(db_session):
    """`_match_thp` has no ORDER BY. Two identical questions must not get two
    different parameters because the planner returned rows in another order."""
    await _seed_v18(db_session)

    first = await evaluate_backend(db_session, "ldl", 120.0, 40)
    second = await evaluate_backend(db_session, "ldl", 120.0, 40)

    assert first is not None and second is not None
    assert first.label == second.label


def test_every_metric_key_has_a_backend_mapping():
    """`_THP_NAMES` and `ranges.py` must stay in step.

    A metric key present in one and not the other silently loses its backend
    range (or never gets one), and nothing else in the codebase would say so.
    """
    from app.health.ranges import RANGES
    from app.health.reference import _THP_NAMES

    missing = set(RANGES) - set(_THP_NAMES)
    assert not missing, (
        f"metrics with no backend parameter mapping: {sorted(missing)}"
    )
    extra = set(_THP_NAMES) - set(RANGES)
    assert not extra, (
        f"backend mappings for metrics ranges.py does not define: {sorted(extra)}"
    )


async def test_unapproved_reference_data_never_grades_a_value(db_session):
    """"HDL/LDL Ratio" ships as status='draft'.

    Reference data the owning team has not approved must not be used to tell a
    patient their value is fine.
    """
    from app.models.coredata import ThpAgeRange, TraditionalHealthParameter

    thp = TraditionalHealthParameter(
        name="LDL Cholesterol", units="mg/dL", aliases=[], status="draft",
        visible=True,
    )
    db_session.add(thp)
    await db_session.flush()
    db_session.add(
        ThpAgeRange(
            thp_id=thp.id, age_min=0, age_max=120, min=0.0, low_danger=0.0,
            low_warn=50.0, ideal=80.0, high_warn=100.0, high_danger=228.0,
            max=228.0,
        )
    )
    await db_session.flush()

    assert await evaluate_backend(db_session, "ldl", 190.0, 40) is None


async def test_a_hidden_parameter_is_not_used(db_session):
    from app.models.coredata import ThpAgeRange, TraditionalHealthParameter

    thp = TraditionalHealthParameter(
        name="LDL Cholesterol", units="mg/dL", aliases=[], status="approved",
        visible=False,
    )
    db_session.add(thp)
    await db_session.flush()
    db_session.add(
        ThpAgeRange(
            thp_id=thp.id, age_min=0, age_max=120, min=0.0, low_danger=0.0,
            low_warn=50.0, ideal=80.0, high_warn=100.0, high_danger=228.0,
            max=228.0,
        )
    )
    await db_session.flush()

    assert await evaluate_backend(db_session, "ldl", 190.0, 40) is None


async def test_a_database_without_the_curation_columns_still_works(db_session):
    """Rows predating status/visible are the curated originals — use them."""
    from app.models.coredata import ThpAgeRange, TraditionalHealthParameter

    thp = TraditionalHealthParameter(
        name="LDL Cholesterol", units="mg/dL", aliases=[], status=None,
        visible=None,
    )
    db_session.add(thp)
    await db_session.flush()
    db_session.add(
        ThpAgeRange(
            thp_id=thp.id, age_min=0, age_max=120, min=0.0, low_danger=0.0,
            low_warn=50.0, ideal=80.0, high_warn=100.0, high_danger=228.0,
            max=228.0,
        )
    )
    await db_session.flush()

    verdict = await evaluate_backend(db_session, "ldl", 190.0, 40)
    assert verdict is not None and verdict.label == "LDL Cholesterol"

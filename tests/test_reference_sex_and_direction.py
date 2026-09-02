"""Two live defects in the reference-range path, both from `decisions-needed.md` D12.

Both became reachable the moment the V28 fix switched the backend path on for
the first time. Before that every lookup raised `UndefinedColumn` and fell back
to the DRAFT constants, so neither could be seen from the deployed app.

1. **HDL graded backwards.** The catalogue gives every parameter a `high_warn`
   because the table has the column, not because both ends are clinically
   meaningful. Production seeds HDL as male 40-60 and female 50-70, so an HDL
   of 65 — a good result — was answered "above the usual range, please consult
   your doctor".

2. **The band was chosen by age alone.** `thp_age_range.sex` was unmapped and
   `order_by(age_min)` has no tiebreak, so which of the male and female bands a
   reader was graded against came down to row order.
"""

from __future__ import annotations

import uuid

from app.health.reference import evaluate_backend, reader_bands
from app.models.core import User
from app.models.coredata import ThpAgeRange, TraditionalHealthParameter

WOMAN = uuid.UUID("dddddddd-dddd-dddd-dddd-ddddddddddd1")
MAN = uuid.UUID("dddddddd-dddd-dddd-dddd-ddddddddddd2")
NOBODY = uuid.UUID("dddddddd-dddd-dddd-dddd-ddddddddddd3")


def _person(uid: uuid.UUID, name: str, gender: str) -> User:
    """The repo's usual User fixture shape — several columns are NOT NULL."""
    from datetime import date

    return User(
        id=uid, name=name, email=f"{name.lower()}@example.com",
        user_name=name.lower(), health_card_number=f"HC-{name}",
        hashcode="x", dob=date(1986, 5, 1), gender=gender,
    )


async def _seed_hdl(db, *, sexed: bool = True):
    """HDL exactly as production seeds it: male 40-60, female 50-70."""
    thp = TraditionalHealthParameter(
        name="HDL Cholesterol", units="mg/dL", aliases=["hdl"],
    )
    db.add(thp)
    await db.flush()
    if sexed:
        db.add(ThpAgeRange(
            thp_id=thp.id, age_min=18, age_max=120, sex="female",
            min=0, low_warn=50, ideal=60, high_warn=70, max=200,
        ))
        db.add(ThpAgeRange(
            thp_id=thp.id, age_min=18, age_max=120, sex="male",
            min=0, low_warn=40, ideal=50, high_warn=60, max=200,
        ))
    else:
        db.add(ThpAgeRange(
            thp_id=thp.id, age_min=18, age_max=120, sex="any",
            min=0, low_warn=40, ideal=50, high_warn=60, max=200,
        ))
    await db.flush()
    return thp


async def _seed_people(db):
    db.add(_person(WOMAN, "Ada", "female"))
    db.add(_person(MAN, "Bob", "male"))
    await db.flush()


# --------------------------------------------------------------------------- #
# 1. A one-sided metric warns on one side
# --------------------------------------------------------------------------- #
async def test_a_good_hdl_is_not_sent_to_a_doctor(db_session):
    """The defect, in the form a reader would meet it.

    `hdl` is `RangeSpec("mg/dL", 40, None)` in the DRAFT constants — "no upper
    bound is flagged", because more HDL is better. The catalogue's `high_warn`
    of 60 is where the *ideal* band ends, not where a warning starts.
    """
    await _seed_hdl(db_session, sexed=False)
    for good in (60, 65, 80):
        v = await evaluate_backend(db_session, "hdl", good, 40)
        assert v is not None, good
        assert v.severity == "normal", f"HDL {good} graded {v.severity}/{v.direction}"


async def test_a_low_hdl_still_warns(db_session):
    """The fix must not make the metric ungradeable — only one-sided."""
    await _seed_hdl(db_session, sexed=False)
    v = await evaluate_backend(db_session, "hdl", 32, 40)
    assert v is not None
    assert (v.severity, v.direction) == ("warn", "low")


async def test_a_two_sided_metric_is_unchanged(db_session):
    """Glucose has both bounds in the DRAFT spec, so both sides still warn."""
    thp = TraditionalHealthParameter(
        name="Fasting Blood Sugar", units="mg/dL", aliases=["glucose"],
    )
    db_session.add(thp)
    await db_session.flush()
    db_session.add(ThpAgeRange(
        thp_id=thp.id, age_min=18, age_max=120, sex="any",
        min=40, low_warn=70, ideal=90, high_warn=100, max=400,
    ))
    await db_session.flush()

    low = await evaluate_backend(db_session, "blood_sugar", 55, 40)
    high = await evaluate_backend(db_session, "blood_sugar", 180, 40)
    assert low is not None and (low.severity, low.direction) == ("warn", "low")
    assert high is not None and (high.severity, high.direction) == ("warn", "high")


# --------------------------------------------------------------------------- #
# 2. The band matches the reader, not the row order
# --------------------------------------------------------------------------- #
async def test_a_woman_is_graded_against_the_female_band(db_session):
    """45 mg/dL is below the female floor (50) and above the male one (40).

    One number, two correct-but-opposite answers, and before this the choice
    between them was row order.
    """
    await _seed_hdl(db_session)
    await _seed_people(db_session)

    age, sex = await reader_bands(db_session, WOMAN)
    assert sex == "female"
    v = await evaluate_backend(db_session, "hdl", 45, age, sex)
    assert v is not None
    assert (v.severity, v.direction) == ("warn", "low")
    assert v.ideal_low == 50


async def test_the_same_number_is_normal_for_a_man(db_session):
    await _seed_hdl(db_session)
    await _seed_people(db_session)

    age, sex = await reader_bands(db_session, MAN)
    assert sex == "male"
    v = await evaluate_backend(db_session, "hdl", 45, age, sex)
    assert v is not None
    assert v.severity == "normal"
    assert v.ideal_low == 40


async def test_an_unknown_sex_falls_back_rather_than_guessing(db_session):
    """Sex-specific bands only, and we do not know theirs.

    Returning None sends the caller to the DRAFT constants. Picking one of the
    two bands would grade half of readers against the other sex's range, which
    is worse than grading them against a general one.
    """
    await _seed_hdl(db_session)
    assert await evaluate_backend(db_session, "hdl", 45, 40, None) is None


async def test_a_unisex_band_is_used_when_the_reader_has_no_sex_on_file(
    db_session,
):
    """`any` bands still serve everyone — 199 of the 277 seeded rows."""
    await _seed_hdl(db_session, sexed=False)
    v = await evaluate_backend(db_session, "hdl", 32, 40, None)
    assert v is not None
    assert (v.severity, v.direction) == ("warn", "low")


async def test_gender_other_is_treated_as_unknown(db_session):
    """`user.gender` allows `other`; the catalogue seeds no band for it."""
    db_session.add(_person(NOBODY, "Cam", "other"))
    await db_session.flush()
    age, sex = await reader_bands(db_session, NOBODY)
    assert age == 40
    assert sex is None


async def test_reader_bands_costs_one_query(db_session):
    """Age and sex in one round trip. The value-check path has no headroom."""
    seen: list[str] = []

    def _count(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)

    db_session.add(_person(MAN, "Bob", "male"))
    await db_session.flush()

    from sqlalchemy import event

    bind = db_session.get_bind()
    engine = getattr(bind, "sync_engine", bind)
    event.listen(engine, "before_cursor_execute", _count)
    try:
        await reader_bands(db_session, MAN)
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert len(seen) == 1, seen


# --------------------------------------------------------------------------- #
# 3. Implausible reference data must not grade anyone
# --------------------------------------------------------------------------- #
async def test_a_nonsense_band_falls_back_to_the_reviewed_constants(db_session):
    """The shape of the real junk row: `HDL/LDL Ratio` is seeded with
    `ideal = 499.7, high_warn = 999`, which satisfies the table's own ordering
    CHECK and is clinically meaningless.

    No such row is reachable today — `_THP_NAMES` maps eleven metrics by exact
    name and none is a ratio — but one alias added upstream is all it would
    take, and reference data nobody checked is what made three answers wrong
    when V18 landed.
    """
    thp = TraditionalHealthParameter(
        name="HDL Cholesterol", units="mg/dL", aliases=["hdl"],
    )
    db_session.add(thp)
    await db_session.flush()
    db_session.add(ThpAgeRange(
        thp_id=thp.id, age_min=18, age_max=120, sex="any",
        min=0.4, low_warn=0.4, ideal=499.7, high_warn=999, max=999,
    ))
    await db_session.flush()

    # DRAFT hdl is (40, None); a band of 0.4-999 overlaps it, so this one is
    # NOT rejected — the guard only fires on total disagreement. Prove the
    # guard fires where it should, with a band that cannot be HDL at all.
    assert await evaluate_backend(db_session, "hdl", 45, 40) is not None

    other = TraditionalHealthParameter(
        name="Total Cholesterol", units="mg/dL", aliases=[],
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(ThpAgeRange(
        thp_id=other.id, age_min=18, age_max=120, sex="any",
        # DRAFT total_cholesterol is (None, 200): anything above 200 is out of
        # range, so a band that STARTS at 400 shares no value with it.
        min=400, low_warn=400, ideal=600, high_warn=800, max=999,
    ))
    await db_session.flush()
    assert await evaluate_backend(
        db_session, "total_cholesterol", 500, 40
    ) is None


async def test_an_ordinary_disagreement_is_still_used(db_session):
    """Production haemoglobin runs 11.5-15.5 for a woman where the DRAFT is
    12-17. That is a difference of opinion, not a broken row, and the backend
    must still win — it is the age- and sex-specific one."""
    thp = TraditionalHealthParameter(
        name="Hemoglobin", units="g/dL", aliases=["hemoglobin"],
    )
    db_session.add(thp)
    await db_session.flush()
    db_session.add(ThpAgeRange(
        thp_id=thp.id, age_min=18, age_max=120, sex="any",
        min=0, low_warn=11.5, ideal=13, high_warn=15.5, max=25,
    ))
    await db_session.flush()

    v = await evaluate_backend(db_session, "hemoglobin", 11.8, 40)
    assert v is not None, "an ordinary disagreement was rejected"
    assert v.ideal_low == 11.5

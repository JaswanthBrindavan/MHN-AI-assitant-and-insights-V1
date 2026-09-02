"""Davi and mhn-spring share one database, and one Flyway chain builds it.

**This check had never once run.** It was described in CLAUDE.md and the README,
and it could not have passed: `db/existing_schema.sql` was composed with Davi's
own adopted migrations held out, and mhn-spring's V14 and V19 both REFERENCE
`drug_reference`, which Davi's V6 creates. The file had no loadable form, so
every "coexistence is verified" claim in this repo rested on a test that threw
before its first assertion.

The premise was wrong, not just the file. Adoption went both ways: production
applies ONE ordered chain — V1..V6(davi)..V19..V21(davi)..V41 — so "lay down
their schema, then run Davi's chain on top" describes nothing that happens
anywhere. The dump is now that whole chain, and these tests check what is
actually true of it.

One more thing the chain alone cannot do: mhn-spring runs Hibernate ddl-auto
beside Flyway, so seven columns across three tables exist in production that no
migration creates — and V25 builds an index on one of them
(`insurance.to_date`). `scripts/build_existing_schema` emits those explicitly.
They were found by diffing the live database against everything the chain
creates, which is the only way to know.

Marked ``pg``: the dump uses enums, ``jsonb``, generated columns and partial
indexes that SQLite does not have.

    TEST_ALEMBIC_URL=postgresql+psycopg2://davi:davi@localhost:5433/davi \\
        pytest -m pg tests/test_coexistence.py
"""

from __future__ import annotations

import os
import pathlib

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db import Base
from app.models.core import EXTERNAL_TABLES

SCHEMA_PRESENT = (
    pathlib.Path(__file__).resolve().parent.parent / "db" / "existing_schema.sql"
).is_file()

pytestmark = [
    pytest.mark.pg,
    # `db/` is gitignored. Regenerate the dump with
    # `python -m scripts.build_existing_schema` (it needs the mhn-spring
    # checkout) before running this.
    pytest.mark.skipif(
        not SCHEMA_PRESENT,
        reason=(
            "db/existing_schema.sql absent (gitignored); regenerate with "
            "python -m scripts.build_existing_schema"
        ),
    ),
]

PG_URL = os.environ.get("TEST_ALEMBIC_URL")
SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "db" / "existing_schema.sql"

# Tables Davi contributed to the shared chain, as its ADOPTED Flyway files.
# They are production's now: mhn-spring's own migrations depend on them.
DAVI_ADOPTED_TABLES = (
    "drug_reference",
    "conversation_sessions",
    "conversation_messages",
)

# Tables that come from mhn-spring.
SPRING_TABLES = (
    "user",
    "reports",
    "vital_reading",
    "medicine_tracking",
    "traditional_health_parameters",
    "thp_age_range",
    "medical_condition",
    "report_parameter_value",
    "sleep_sessions",
    "medicine_dose_log",
    "lifestyle_daily_total",
    "sahha_daily_total",
)


def _load_production_schema(engine) -> None:
    """Drop everything, then apply the whole Flyway chain."""
    sql = SCHEMA.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        # One statement stream: the chain is ordered, and splitting it would
        # break the DO $$ blocks and multi-line inserts.
        #
        # Straight down to the DBAPI cursor. `exec_driver_sql(sql)` hands
        # psycopg2 an `immutabledict()` for parameters and it raises
        # "immutabledict is not a sequence"; `exec_driver_sql(sql, ())` then
        # trips over the `%` signs in the dump. Neither ever worked, which is
        # part of why this check had never actually run.
        conn.connection.dbapi_connection.cursor().execute(sql)


@pytest.mark.skipif(not PG_URL, reason="TEST_ALEMBIC_URL not set")
def test_the_production_chain_applies_to_an_empty_database():
    """The check that was documented for months and never performed.

    If this fails, the two teams' migrations have genuinely stopped composing —
    a missing dependency, a renamed column indexed by a later file, a type that
    no longer exists. It is the only place that is discoverable before deploy.
    """
    assert PG_URL is not None
    engine = create_engine(PG_URL)
    _load_production_schema(engine)
    tables = set(inspect(engine).get_table_names())
    assert len(tables) > 80, f"only {len(tables)} tables — the chain stopped early"
    engine.dispose()


@pytest.mark.skipif(not PG_URL, reason="TEST_ALEMBIC_URL not set")
def test_both_teams_tables_survive_the_whole_chain():
    """Adopted is not the same as absorbed.

    Davi's V6 and V21 are in mhn-spring's chain now, so a migration of theirs
    could drop or rename one and nothing here would notice until a read failed
    in production. That is exactly how V35's rename of
    `lifestyle_daily_total.log_type` reached us.
    """
    assert PG_URL is not None
    engine = create_engine(PG_URL)
    _load_production_schema(engine)
    tables = set(inspect(engine).get_table_names())

    missing_theirs = [t for t in SPRING_TABLES if t not in tables]
    missing_ours = [t for t in DAVI_ADOPTED_TABLES if t not in tables]
    assert not missing_theirs, f"mhn-spring tables absent: {missing_theirs}"
    assert not missing_ours, f"Davi's adopted tables absent: {missing_ours}"
    engine.dispose()


@pytest.mark.skipif(not PG_URL, reason="TEST_ALEMBIC_URL not set")
def test_davi_maps_no_column_production_does_not_have():
    """The V28 and V35 class of bug, checked against a REAL database.

    `tests/test_schema_parity.py` asks the same question of the dump by
    parsing it. This asks PostgreSQL, so it also covers what a parser cannot
    see: a column that exists but under a different type, an enum the chain
    never created, a rename that a regex missed.

    Both V28 (dropped `low_danger`/`high_danger`) and V35 (renamed `log_type`
    to `metric`) shipped to production while every check here reported green.
    """
    assert PG_URL is not None
    engine = create_engine(PG_URL)
    _load_production_schema(engine)
    inspector = inspect(engine)
    live = set(inspector.get_table_names())

    problems: list[str] = []
    checked = 0
    for name in sorted(EXTERNAL_TABLES):
        table = Base.metadata.tables.get(name)
        if table is None or name not in live:
            continue
        actual = {c["name"] for c in inspector.get_columns(name)}
        mapped = {c.name for c in table.columns}
        missing = sorted(mapped - actual)
        if missing:
            problems.append(f"{name}: model maps {missing}, production does not")
        checked += 1

    assert checked > 10, f"only {checked} external tables checked — wiring is off"
    assert not problems, "\n".join(problems)
    engine.dispose()


@pytest.mark.skipif(not PG_URL, reason="TEST_ALEMBIC_URL not set")
def test_the_reference_catalogue_is_present_and_populated():
    """V18's catalogue is data, not decoration.

    An empty catalogue is what made Davi's value-check answer three questions
    wrong, so a schema file without the inserts would let that class of bug
    through again unseen.
    """
    assert PG_URL is not None
    engine = create_engine(PG_URL)
    _load_production_schema(engine)

    with engine.begin() as conn:
        params = conn.execute(
            text("SELECT count(*) FROM traditional_health_parameters")
        ).scalar()
        bands = conn.execute(text("SELECT count(*) FROM thp_age_range")).scalar()
        sexed = conn.execute(
            text("SELECT count(*) FROM thp_age_range WHERE sex <> 'any'")
        ).scalar()

    assert params and params > 100, (
        f"only {params} reference parameters — the catalogue inserts are missing"
    )
    assert bands and bands > 100, f"only {bands} age bands"
    # The sex-specific bands are why `ThpAgeRange.sex` had to be mapped: an
    # HDL of 45 warns for a woman and is normal for a man.
    assert sexed and sexed > 50, (
        f"only {sexed} sex-specific bands — D12's fix has nothing to select on"
    )
    engine.dispose()

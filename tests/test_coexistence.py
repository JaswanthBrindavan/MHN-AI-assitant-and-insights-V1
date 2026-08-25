"""Davi's migrations must run on top of mhn-spring's schema, not beside it.

CLAUDE.md and README both describe this check. **No test performed it** — it
was run by hand, against a `db/existing_schema.sql` that was still the V1
baseline. So everything mhn-spring changed in V7–V19 was invisible to every
automated check in this repository, which is how V18's reference catalogue came
to silently break three patient-facing answers before anyone noticed.

The dump is now composed from that chain by
``python -m scripts.build_existing_schema``. This test loads it and then runs
Davi's Alembic chain on top, which is what production actually looks like:
Flyway lays down the shared schema, and Davi's tables have to fit alongside it
without colliding on a table, an index, a constraint or a type.

Marked ``pg``: it needs a real PostgreSQL, because the dump uses enums,
``jsonb``, generated columns and partial indexes that SQLite does not have.

    TEST_ALEMBIC_URL=postgresql+psycopg2://davi:davi@localhost:5432/davi \\
        pytest -m pg tests/test_coexistence.py
"""

from __future__ import annotations

import os
import pathlib

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.pg

PG_URL = os.environ.get("TEST_ALEMBIC_URL")
SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "db" / "existing_schema.sql"

# Tables Davi's Alembic chain creates. If Flyway ever starts creating one of
# these under the same name, the two chains have collided and this test is how
# you find out.
DAVI_TABLES = (
    "pedigree_members",
    "pedigree_conditions",
    "insight_artifacts",
    "mcp_chunks",
    "user_profiles",
    "turn_feedback",
    "clinician_reviewers",
    "insight_review_audit",
    "erasure_requests",
)

# Tables that come from mhn-spring and must survive Davi's chain untouched.
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
)


def _alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "app/alembic")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _load_production_schema(engine) -> None:
    """Drop everything, then lay down mhn-spring's schema."""
    sql = SCHEMA.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        # One statement stream: the dump is ordered, and splitting it would
        # break the DO $$ blocks and multi-line inserts.
        conn.exec_driver_sql(sql)


@pytest.mark.skipif(not PG_URL, reason="TEST_ALEMBIC_URL not set")
def test_davi_migrations_apply_on_top_of_the_production_schema():
    """The check that was documented but never run."""
    assert PG_URL is not None
    engine = create_engine(PG_URL)
    _load_production_schema(engine)

    before = set(inspect(engine).get_table_names())
    for table in SPRING_TABLES:
        assert table in before, (
            f"{table} missing from db/existing_schema.sql — regenerate it with "
            "python -m scripts.build_existing_schema"
        )

    # The whole point: Davi's chain on top of theirs, with no collision.
    command.upgrade(_alembic_config(PG_URL), "head")

    after = set(inspect(engine).get_table_names())
    for table in DAVI_TABLES:
        assert table in after, f"Davi's chain did not create {table}"
    for table in SPRING_TABLES:
        assert table in after, f"Davi's chain destroyed {table}"

    engine.dispose()


@pytest.mark.skipif(not PG_URL, reason="TEST_ALEMBIC_URL not set")
def test_davi_adds_no_column_to_a_table_it_does_not_own():
    """Davi READS production tables; it does not reshape them.

    A partial mapping that grew a column would silently diverge from what
    mhn-spring writes, and the two services would disagree about the same row.
    """
    assert PG_URL is not None
    engine = create_engine(PG_URL)
    _load_production_schema(engine)

    inspector = inspect(engine)
    before = {t: {c["name"] for c in inspector.get_columns(t)} for t in SPRING_TABLES}

    command.upgrade(_alembic_config(PG_URL), "head")

    inspector = inspect(engine)
    changed = {}
    for table, columns in before.items():
        now = {c["name"] for c in inspector.get_columns(table)}
        added = now - columns
        if added:
            changed[table] = sorted(added)
    assert not changed, f"Davi's chain altered tables it does not own: {changed}"

    engine.dispose()


@pytest.mark.skipif(not PG_URL, reason="TEST_ALEMBIC_URL not set")
def test_the_reference_catalogue_is_present_and_populated():
    """V18's catalogue is data, not decoration.

    It is what made Davi's value-check answer wrong, so a schema file without
    it would let that class of bug through again unseen.
    """
    assert PG_URL is not None
    engine = create_engine(PG_URL)
    _load_production_schema(engine)

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM traditional_health_parameters")
        ).scalar()
    assert count and count > 100, (
        f"only {count} reference parameters — the catalogue inserts are missing"
    )
    engine.dispose()

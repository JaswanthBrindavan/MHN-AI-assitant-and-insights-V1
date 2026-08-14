"""Phase 1 — Alembic migration reversibility on real PostgreSQL.

Marked ``pg`` so it is deselected by default. Provide a real Postgres URL via
``TEST_ALEMBIC_URL`` (sync/psycopg2 driver) to run it, e.g.::

    TEST_ALEMBIC_URL=postgresql+psycopg2://davi:davi@localhost:5432/davi \\
        pytest -m pg tests/test_migrations.py
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

pytestmark = pytest.mark.pg

PG_URL = os.environ.get("TEST_ALEMBIC_URL")


def _alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "app/alembic")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.mark.skipif(not PG_URL, reason="TEST_ALEMBIC_URL not set")
def test_migration_upgrade_downgrade_upgrade():
    assert PG_URL is not None
    cfg = _alembic_config(PG_URL)
    engine = create_engine(PG_URL)

    # Clean slate.
    command.downgrade(cfg, "base")

    command.upgrade(cfg, "head")
    tables_after_upgrade = set(inspect(engine).get_table_names())
    assert "pedigree_conditions" in tables_after_upgrade
    assert "mcp_chunks" in tables_after_upgrade
    assert "insight_artifacts" in tables_after_upgrade

    # Downgrade must remove every table the migration created.
    command.downgrade(cfg, "base")
    tables_after_downgrade = set(inspect(engine).get_table_names())
    for t in ("mcp_chunks", "insight_artifacts", "pedigree_conditions"):
        assert t not in tables_after_downgrade

    # And upgrade must be repeatable (proves downgrade left a clean slate).
    command.upgrade(cfg, "head")
    tables_second = set(inspect(engine).get_table_names())
    assert "pedigree_conditions" in tables_second
    engine.dispose()

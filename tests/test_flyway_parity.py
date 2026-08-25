"""The Flyway files are what production actually gets. Nothing checked them.

CLAUDE.md: "Production schema ships as Flyway" — `db/flyway/V*__davi_*.sql`
is the DDL adopted into mhn-spring's chain, while Davi's Alembic chain builds
local and test databases only. That split means the test suite runs entirely
against schema the Alembic chain produced, and a Flyway file that drifted from
the models would pass every test here and fail only in production, as a
missing column at runtime.

This compares the two for Davi's own tables. It is deliberately shallow —
column NAMES and nullability, not types — because a name mismatch is the
failure mode that silently ships, and a shallow check that runs beats a deep
one that needs a live PostgreSQL.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.db import Base

FLYWAY_DIR = pathlib.Path(__file__).resolve().parent.parent / "db" / "flyway"

# Davi tables that ship as Flyway DDL, mapped to the migration that creates
# them. V6 predates this check and is covered by the coexistence test.
FLYWAY_TABLES = {
    "user_profiles": "V7__davi_user_profile.sql",
    "turn_feedback": "V8__davi_feedback.sql",
}


def _create_table_columns(sql: str, table: str) -> dict[str, bool]:
    """Column name -> nullable, parsed from a CREATE TABLE block.

    Crude regex parsing. Justified: these files are hand-written by us in a
    fixed style, and the alternative (a live PostgreSQL) would make the check
    conditional — which is how V6 and V7 came to have no check at all.
    """
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS\s+(?:public\.)?{table}\s*\((.*?)\n\);",
        sql,
        re.DOTALL | re.IGNORECASE,
    )
    assert match, f"no CREATE TABLE for {table}"

    columns: dict[str, bool] = {}
    for raw in match.group(1).split("\n"):
        line = raw.split("--")[0].strip().rstrip(",")
        if not line:
            continue
        # Skip table-level constraints (PRIMARY KEY (...), UNIQUE (...), ...).
        if re.match(r"(PRIMARY|UNIQUE|FOREIGN|CONSTRAINT|CHECK)\b", line, re.I):
            continue
        name = line.split()[0].strip('"')
        not_null = bool(re.search(r"\bNOT NULL\b", line, re.I)) or bool(
            re.search(r"\bPRIMARY KEY\b", line, re.I)
        )
        columns[name] = not not_null
    return columns


@pytest.mark.parametrize(("table", "filename"), sorted(FLYWAY_TABLES.items()))
def test_flyway_ddl_matches_the_model(table: str, filename: str):
    path = FLYWAY_DIR / filename
    assert path.exists(), f"{filename} is missing — production would not get {table}"
    flyway_columns = _create_table_columns(path.read_text(encoding="utf-8"), table)

    model = Base.metadata.tables[table]
    model_columns = {c.name: c.nullable for c in model.columns}

    missing = set(model_columns) - set(flyway_columns)
    extra = set(flyway_columns) - set(model_columns)
    assert not missing, f"{filename} is missing columns the code reads: {sorted(missing)}"
    assert not extra, f"{filename} declares columns no model has: {sorted(extra)}"

    for name, nullable in model_columns.items():
        assert flyway_columns[name] == nullable, (
            f"{filename}.{name} nullability disagrees with the model "
            f"(flyway nullable={flyway_columns[name]}, model nullable={nullable})"
        )


@pytest.mark.parametrize("filename", sorted(set(FLYWAY_TABLES.values())))
def test_flyway_files_are_idempotent(filename: str):
    """Every Davi Flyway file must survive a database Alembic already built.

    The RUN_MIGRATIONS_ON_START shortcut means a staging database can have the
    table already; a bare CREATE TABLE would abort the whole Flyway chain.
    """
    sql = (FLYWAY_DIR / filename).read_text(encoding="utf-8")
    creates = re.findall(r"CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX)\s+(\w*)", sql, re.I)
    assert creates, f"{filename} creates nothing"
    for statement in re.finditer(
        r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\b[^;]*", sql, re.I
    ):
        text = statement.group(0)
        assert re.search(r"IF NOT EXISTS", text, re.I), (
            f"{filename} has a CREATE without IF NOT EXISTS:\n{text[:120]}"
        )


def test_no_davi_flyway_file_is_left_unchecked():
    """A new V*__davi_*.sql must be added to FLYWAY_TABLES.

    Without this, the parity check silently stops covering new tables — the
    same way V6 and V7 shipped with no check at all.
    """
    known = set(FLYWAY_TABLES.values()) | {"V6__davi_ai_tables.sql"}
    on_disk = {p.name for p in FLYWAY_DIR.glob("V*__davi_*.sql")}
    unchecked = on_disk - known
    assert not unchecked, (
        f"unchecked Flyway files: {sorted(unchecked)} — add each table to "
        "FLYWAY_TABLES so parity with the model is verified"
    )

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

import os
import pathlib
import re

import pytest

from app.db import Base

FLYWAY_DIR = pathlib.Path(__file__).resolve().parent.parent / "db" / "flyway"

# `db/` is gitignored, so a fresh clone does not have these files and this
# whole guard has nothing to check. Skipping is honest; failing would just
# teach people to ignore a red build.
#
# NOTE THE COST, because it is real: this guard is what caught Davi's V7-V10
# colliding with mhn-spring's chain, and what catches a Flyway file drifting
# from the models. Wherever the DDL actually lives, run this there — the
# migrations must be checked SOMEWHERE.
pytestmark = pytest.mark.skipif(
    not FLYWAY_DIR.is_dir(),
    reason="db/flyway is not in this checkout (gitignored); nothing to verify",
)

# Davi tables that ship as Flyway DDL, mapped to the migration that creates
# them. V6 predates this check and is covered by the coexistence test.
# Every Davi migration after V6 is consolidated into ONE file, now ADOPTED into
# mhn-spring as V21__davi_chat_platform.sql. It was staged here as V7-V10, then
# V20 — both collided, because Flyway version numbers are a shared namespace
# and this repo cannot see the other team's chain. The collision test below is
# what caught the second one, a day after it caught the first.
#
# db/ is gitignored: mhn-spring owns these files now. What remains here is a
# staging copy, and this guard exists to stop it drifting from the model.
FLYWAY_TABLES = {
    "user_profiles": "V21__davi_chat_platform.sql",
    "turn_feedback": "V21__davi_chat_platform.sql",
    "clinician_reviewers": "V21__davi_chat_platform.sql",
    "insight_review_audit": "V21__davi_chat_platform.sql",
    "erasure_requests": "V21__davi_chat_platform.sql",
    "user_memory_document": "V21__davi_chat_platform.sql",
    # Created in V6, gains actor_user_id in V21 — the case _added_columns
    # exists for.
    "job_runs": "V6__davi_ai_tables.sql",
    "pattern_artifacts": "V43__davi_pattern_artifacts.sql",
}

# Migrations that create no table, so there is nothing for the column-parity
# check to compare. Listed explicitly rather than skipped by pattern: a file
# lands here only by a deliberate edit, so a new TABLE can never slip through
# by being named like an index migration.
INDEX_ONLY_FILES = {
    "V23__davi_conversation_message_index.sql",
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


def _added_columns(table: str) -> dict[str, bool]:
    """Columns a LATER migration bolted on with ALTER TABLE ... ADD COLUMN.

    Without this the check reads only the original CREATE TABLE and reports a
    column added by a subsequent migration as MISSING from production — or,
    worse, silently passes a table whose later columns were never checked at
    all. Scans every Flyway file, because a column can be added by any of them.
    """
    added: dict[str, bool] = {}
    for path in sorted(FLYWAY_DIR.glob("V*.sql")):
        sql = path.read_text(encoding="utf-8")
        for match in re.finditer(
            rf"ALTER TABLE\s+(?:ONLY\s+)?(?:public\.)?{table}\s+"
            rf"ADD COLUMN(?:\s+IF NOT EXISTS)?\s+(\w+)([^;]*);",
            sql,
            re.IGNORECASE,
        ):
            name, rest = match.group(1), match.group(2)
            added[name] = not re.search(r"NOT NULL", rest, re.I)
    return added


@pytest.mark.parametrize(("table", "filename"), sorted(FLYWAY_TABLES.items()))
def test_flyway_ddl_matches_the_model(table: str, filename: str):
    path = FLYWAY_DIR / filename
    assert path.exists(), f"{filename} is missing — production would not get {table}"
    flyway_columns = _create_table_columns(path.read_text(encoding="utf-8"), table)
    # A column added by a later migration is just as present in production.
    flyway_columns.update(_added_columns(table))

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
    known = (
        set(FLYWAY_TABLES.values())
        | {"V6__davi_ai_tables.sql"}
        | INDEX_ONLY_FILES
    )
    on_disk = {p.name for p in FLYWAY_DIR.glob("V*__davi_*.sql")}
    unchecked = on_disk - known
    assert not unchecked, (
        f"unchecked Flyway files: {sorted(unchecked)} — add each table to "
        "FLYWAY_TABLES so parity with the model is verified"
    )


# --------------------------------------------------------------------------- #
# Version collision with mhn-spring's chain
# --------------------------------------------------------------------------- #
# Flyway version numbers are a SHARED namespace. Davi stages DDL that another
# team applies into their chain, so a number Davi picks is only free until
# somebody there picks it too — and nothing in this repository can see that.
#
# It happened: Davi staged V7-V10 while mhn-spring used those same numbers for
# medical_history, medical_history_date_order, period_pause_and_pregnancy and
# ai_name_check. Four migrations that could never have been applied, discovered
# only by opening the other repository.
_SPRING_MIGRATIONS = "src/main/resources/db/migration"


def _spring_dir() -> pathlib.Path | None:
    """mhn-spring's migration directory, if this machine has the repo."""
    override = os.environ.get("MHN_SPRING_PATH")
    candidates = [pathlib.Path(override)] if override else []
    # The usual sibling checkouts.
    root = FLYWAY_DIR.parent.parent.parent
    candidates += [root / "mhn-spring-main", root / "mhn-spring"]
    for base in candidates:
        directory = base / _SPRING_MIGRATIONS
        if directory.is_dir():
            return directory
    return None


def _versions(directory: pathlib.Path) -> dict[int, str]:
    found: dict[int, str] = {}
    for path in directory.glob("V*__*.sql"):
        match = re.match(r"V(\d+)__", path.name)
        if match:
            found[int(match.group(1))] = path.name
    return found


def test_davi_migration_numbers_are_unique_among_themselves():
    """Cheap, and runs everywhere."""
    numbers = _versions(FLYWAY_DIR)
    files = sorted(p.name for p in FLYWAY_DIR.glob("V*__*.sql"))
    assert len(numbers) == len(files), f"duplicate version number among {files}"


def test_davi_migrations_do_not_collide_with_mhn_spring():
    """The check that would have caught it. Skips where the repo is absent.

    Set MHN_SPRING_PATH to run this against a checkout elsewhere.
    """
    spring_dir = _spring_dir()
    if spring_dir is None:
        pytest.skip(
            "mhn-spring not found; set MHN_SPRING_PATH to check for collisions"
        )

    spring = _versions(spring_dir)
    davi = _versions(FLYWAY_DIR)

    collisions = {
        version: (davi[version], spring[version])
        for version in sorted(set(davi) & set(spring))
        # V6 IS the adopted Davi file — the same number and the same file.
        if davi[version] != spring[version]
    }
    assert not collisions, (
        "Davi migration numbers already used in mhn-spring's chain: "
        + "; ".join(
            f"V{v}: davi has {d!r}, spring has {s!r}"
            for v, (d, s) in collisions.items()
        )
        + ". Renumber above mhn-spring's head."
    )

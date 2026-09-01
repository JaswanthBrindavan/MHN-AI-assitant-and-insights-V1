"""A mapped column the production schema does not have is a runtime outage.

This is the guard that was missing. mhn-spring's `V28__thp_age_range_three_zones`
dropped `thp_age_range.low_danger` and `high_danger`; `app/models/coredata.py`
went on mapping both, so `select(ThpAgeRange)` named two columns that no longer
existed and every backend reference-range lookup raised `UndefinedColumn` — on
a patient-safety path, swallowed by a fail-open `except`, with nothing in this
repository able to notice. The external tables are in `EXTERNAL_TABLES`, so
they are outside Alembic, outside `test_flyway_parity.py`, and the aiosqlite
suite builds them from the models themselves: the models cannot disagree with a
schema they define. Every test stayed green.

So compare the models to mhn-spring's schema directly. `db/existing_schema.sql`
is their V1..V41 chain replayed in order by `scripts.build_existing_schema`, so
the effective column set is CREATE TABLE plus every later ADD/DROP COLUMN — the
drop is what matters, and only a replay sees it.

One direction only: the models are deliberately PARTIAL (they map the columns
Davi reads and ignore the rest), so extra columns in the schema are fine and
missing ones are the bug.
"""

from __future__ import annotations

import pathlib
import re

import pytest

import app.models.coredata  # noqa: F401  — registers the external tables
from app.db import Base
from app.models.core import EXTERNAL_TABLES

SCHEMA = (
    pathlib.Path(__file__).resolve().parent.parent / "db" / "existing_schema.sql"
)

# `db/` is gitignored — a fresh clone has nothing to compare against, and a red
# build people learn to ignore is worse than an honest skip. Regenerate with
# `python -m scripts.build_existing_schema` (needs the mhn-spring checkout).
pytestmark = pytest.mark.skipif(
    not SCHEMA.is_file(),
    reason=(
        "db/existing_schema.sql absent (gitignored); regenerate with "
        "python -m scripts.build_existing_schema"
    ),
)

# Columns production HAS but the Flyway chain does not create, because
# mhn-spring runs `spring.jpa.hibernate.ddl-auto=update` and Hibernate added
# them from the Java entities. The dump replays Flyway only, so it cannot see
# them. Each entry needs a reason: this list is the one way a real break can
# hide, so it stays short and hand-checked.
DDL_AUTO_COLUMNS = {
    # CLAUDE.md, project_docs/spring-integration-v19.md:292 — the owner-side
    # read grants. Mapped nullable with a fallback for exactly this reason.
    ("family_connect", "req_read"),
    ("family_connect", "acc_read"),
    # app/coredata/service.py:82 — same mechanism, predicted-cycle flags.
    ("period_tracking", "is_predicted"),
    ("period_tracking", "symptoms"),
}

# Whole tables ddl-auto created, so the Flyway dump has no CREATE TABLE at all.
DDL_AUTO_TABLES = {"file_access_exclusions"}


def _effective_columns(sql: str, table: str) -> set[str] | None:
    """Column names after the chain has run, or None if never created.

    Crude regex, same bargain `test_flyway_parity.py` already takes: a shallow
    check that runs beats a deep one that needs a live PostgreSQL and therefore
    never runs. Column NAMES only — a wrong type does not raise
    `UndefinedColumn`.
    """
    create = re.search(
        rf'CREATE TABLE (?:IF NOT EXISTS )?(?:public\.)?"?{table}"?\s*\((.*?)\n\);',
        sql,
        re.S | re.I,
    )
    if create is None:
        return None
    cols: set[str] = set()
    for line in create.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        if re.match(
            r"(?i)(constraint|primary key|unique|foreign key|check)\b", line
        ):
            continue
        cols.add(line.split()[0].strip('"').lower())

    # Then every later ALTER, in file order — the chain is ordered, and a DROP
    # after an ADD has to win.
    for alter in re.finditer(
        rf'(?is)ALTER TABLE (?:ONLY )?(?:public\.)?"?{table}"?\s(.*?);', sql
    ):
        body = alter.group(1)
        for add in re.finditer(
            r'(?i)ADD COLUMN (?:IF NOT EXISTS )?"?(\w+)"?', body
        ):
            cols.add(add.group(1).lower())
        for drop in re.finditer(
            r'(?i)DROP COLUMN (?:IF EXISTS )?"?(\w+)"?', body
        ):
            cols.discard(drop.group(1).lower())
        # A RENAME is a DROP of the old name and an ADD of the new one, and
        # skipping it is how `lifestyle_daily_total.log_type` stayed mapped
        # after mhn-spring's V35 renamed it to `metric`: every read raised
        # UndefinedColumn in production while this guard reported parity.
        # The whole point of the guard is that the other team can move a
        # column without us noticing, and a rename is the quietest way they
        # can do it — nothing is added and nothing is removed on net.
        for ren in re.finditer(
            r'(?i)RENAME COLUMN "?(\w+)"?\s+TO\s+"?(\w+)"?', body
        ):
            cols.discard(ren.group(1).lower())
            cols.add(ren.group(2).lower())
    return cols


def test_every_mapped_external_column_exists_in_production() -> None:
    sql = SCHEMA.read_text(encoding="utf-8")
    checked = 0
    problems: list[str] = []

    for name in sorted(EXTERNAL_TABLES):
        table = Base.metadata.tables.get(name)
        if table is None:  # in EXTERNAL_TABLES but not mapped here
            continue
        if name in DDL_AUTO_TABLES:
            continue
        actual = _effective_columns(sql, name)
        if actual is None:
            problems.append(
                f"{name}: mapped, but db/existing_schema.sql never creates it"
            )
            continue
        checked += 1
        missing = sorted(
            c.name
            for c in table.columns
            if c.name.lower() not in actual
            and (name, c.name) not in DDL_AUTO_COLUMNS
        )
        if missing:
            problems.append(
                f"{name}: model maps {missing}, production does not have "
                f"them — every SELECT on this table raises UndefinedColumn"
            )

    assert not problems, "\n".join(problems)
    # A regex that quietly stops matching would make the whole check vacuous.
    assert checked >= 20, f"only {checked} external tables compared"


def test_the_parser_would_have_caught_v28() -> None:
    """The guard has to see a DROP, not just a CREATE.

    `thp_age_range` is created with `low_danger`/`high_danger` in V1 and loses
    them in V28, ~4,700 lines later. A parser that reads only CREATE TABLE
    passes the test above while production is broken — which is the exact
    failure this file exists to stop.
    """
    cols = _effective_columns(SCHEMA.read_text(encoding="utf-8"), "thp_age_range")
    assert cols is not None
    assert {"low_warn", "high_warn", "min", "max", "ideal"} <= cols
    assert not {"low_danger", "high_danger"} & cols

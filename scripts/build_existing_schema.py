"""Rebuild db/existing_schema.sql from mhn-spring's Flyway chain.

No psql here, so this composes the file from the migrations themselves — which
IS what Flyway applies, in the order it applies them. Reproducible, and it can
be regenerated the moment the other team adds a migration.

Davi's own ADOPTED migrations are INCLUDED, and that is a change.

They used to be excluded, on the reasoning that the coexistence check proves
Davi's Alembic-built tables do not collide with tables Davi does not own, so
including them would collide by construction. That reasoning expired the
moment adoption went both ways: mhn-spring's V14 and V19 both REFERENCE
`drug_reference`, which Davi's own V6 creates. With V6 held out, the composed
file cannot load at all — the FK has no target — so the check it existed to
serve had never once run.

Production applies ONE ordered chain: V1..V6(davi)..V19..V21(davi)..V41. This
file now mirrors that, because a schema file that cannot describe production is
not a description of production. `EXCLUDE` is kept, empty, so the shape of the
decision stays visible if a Davi migration ever does need holding back.
"""
import os
import pathlib
import re

_DEFAULT_SPRING = "mhn-spring-main/src/main/resources/db/migration"
SPRING = pathlib.Path(
    os.environ.get("MHN_SPRING_PATH", "")
) / "src/main/resources/db/migration" if os.environ.get("MHN_SPRING_PATH") else (
    pathlib.Path(__file__).resolve().parent.parent.parent / _DEFAULT_SPRING
)
OUT = pathlib.Path(__file__).resolve().parent.parent / "db" / "existing_schema.sql"


files = []
for path in SPRING.glob("V*__*.sql"):
    match = re.match(r"V(\d+)__(.+)\.sql$", path.name)
    if match:
        files.append((int(match.group(1)), match.group(2), path))
files.sort()

# Davi's own adopted migrations, by name rather than by number.
# Nothing is held back: their chain depends on ours (V14/V19 -> V6's
# drug_reference), so a file without Davi's adopted migrations is not
# loadable and never was. See the module docstring.
EXCLUDE: set[int] = set()

# --------------------------------------------------------------------------- #
# Hibernate ddl-auto columns
# --------------------------------------------------------------------------- #
# mhn-spring runs Hibernate with ddl-auto alongside Flyway, so a handful of
# columns exist in production that NO migration creates. The chain is not
# self-sufficient because of them: V25 builds an index on `insurance.to_date`,
# and nothing in V1..V41 ever adds that column. Compose the chain without them
# and the file cannot load -- which is why this check had never once run.
#
# Verified by diffing the live database against everything the chain creates;
# these seven, across three tables, are the whole set. They are emitted right
# after the migration that creates their tables, so a later migration can
# index them.
DDL_AUTO_AFTER = 19
DDL_AUTO_SQL = """
-- ---------------------------------------------------------------------------
-- Hibernate ddl-auto columns (NOT from any migration; see the build script).
-- ---------------------------------------------------------------------------
ALTER TABLE public.family_connect
    ADD COLUMN IF NOT EXISTS req_read  boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS req_write boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS acc_read  boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS acc_write boolean NOT NULL DEFAULT false;
ALTER TABLE public.insurance
    ADD COLUMN IF NOT EXISTS from_date timestamptz,
    ADD COLUMN IF NOT EXISTS to_date   timestamptz;
ALTER TABLE public.relations
    ADD COLUMN IF NOT EXISTS sort integer;
"""

header = f"""-- db/existing_schema.sql
--
-- The production schema Davi does NOT own, as mhn-spring's Flyway chain
-- defines it. Composed from that chain in application order, NOT a pg_dump —
-- so it can be regenerated whenever the other team adds a migration, and it
-- says exactly which migration each object came from.
--
-- REGENERATE:  python -m scripts.build_existing_schema
--
-- SOURCE: {SPRING}
-- CHAIN:  V{files[0][0]}..V{files[-1][0]}, excluding {", ".join(f"V{n}" for n in sorted(EXCLUDE))}
--         (Davi's own adopted migrations — the coexistence check exists to
--         prove Davi's tables do not collide with the ones Davi does not own,
--         so including them would collide by construction).
--
-- WHY THIS MATTERS. Until now this file was the V1 baseline alone. Everything
-- V7-V19 changed was invisible to every check in this repository — including
-- `traditional_health_parameters`, whose population in V18 silently made three
-- of Davi's patient-facing answers wrong. The tests could not have caught it:
-- tests/conftest.py builds its schema from Davi's OWN partial mappings, so a
-- column Davi does not map does not exist in any test database.
--
-- The DATA inserts (V17 drinks, V18 the 193-parameter reference catalogue,
-- V19 the drug merge) are kept deliberately. They are not decoration: the V18
-- catalogue is precisely what broke the value-check matcher, and a schema file
-- without it would let the same class of bug through again.
--
-- ============================================================================

"""

parts = [header]
for number, name, path in files:
    if number in EXCLUDE:
        parts.append(
            f"-- ----------------------------------------------------------\n"
            f"-- V{number} {name} — SKIPPED (Davi's own adopted migration)\n"
            f"-- ----------------------------------------------------------\n\n"
        )
        continue
    body = path.read_text(encoding="utf-8")
    parts.append(
        f"-- ============================================================\n"
        f"-- V{number}__{name}.sql\n"
        f"-- ============================================================\n"
        f"{body.rstrip()}\n\n"
    )
    if number == DDL_AUTO_AFTER:
        parts.append(DDL_AUTO_SQL.strip() + "\n\n")

OUT.write_text("".join(parts), encoding="utf-8")
lines = OUT.read_text(encoding="utf-8").count("\n")
print(f"wrote {OUT} — {lines:,} lines from {len(files) - len(EXCLUDE)} migrations")
print("chain:", ", ".join(f"V{n}" for n, _, _ in files if n not in EXCLUDE))

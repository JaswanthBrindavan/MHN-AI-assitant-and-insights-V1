"""Rebuild db/existing_schema.sql from mhn-spring's Flyway chain.

No psql here, so this composes the file from the migrations themselves — which
IS what Flyway applies, in the order it applies them. Reproducible, and it can
be regenerated the moment the other team adds a migration.

V6 is EXCLUDED: it is Davi's own adopted file, and the coexistence check exists
to prove Davi's Alembic-built tables do not collide with the tables Davi does
NOT own. Including it would collide by construction.
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

EXCLUDE = {6}  # Davi's own adopted migration

files = []
for path in SPRING.glob("V*__*.sql"):
    match = re.match(r"V(\d+)__(.+)\.sql$", path.name)
    if match:
        files.append((int(match.group(1)), match.group(2), path))
files.sort()

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
-- CHAIN:  V{files[0][0]}..V{files[-1][0]}, excluding V6 (Davi's own adopted
--         migration — the coexistence check exists to prove Davi's tables do
--         not collide with the ones Davi does not own, so including it would
--         collide by construction).
--
-- WHY THIS MATTERS. Until now this file was the V1 baseline alone. Everything
-- V7-V19 changed was invisible to every check in this repository — including
-- `traditional_health_parameters`, whose population in V18 silently made three
-- of Davi's patient-facing answers wrong. The tests could not have caught it:
-- tests/conftest.py builds its schema from Davi's OWN partial mappings, so a
-- column Davi does not map does not exist in any test database.
--
-- The DATA inserts (V17 drinks, V18 the 192-parameter reference catalogue,
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

OUT.write_text("".join(parts), encoding="utf-8")
lines = OUT.read_text(encoding="utf-8").count("\n")
print(f"wrote {OUT} — {lines:,} lines from {len(files) - len(EXCLUDE)} migrations")
print("chain:", ", ".join(f"V{n}" for n, _, _ in files if n not in EXCLUDE))

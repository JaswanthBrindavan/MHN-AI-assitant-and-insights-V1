"""Refuse to run seed scripts against a non-local database.

Seed scripts write SYNTHETIC users and data into the shared core tables
(including ``"user"``). On the production database that is data corruption.
This guard blocks the CLI entrypoints whenever DATABASE_URL points at a
non-local host, unless ALLOW_REMOTE_SEED=true is set explicitly.

Programmatic use from tests (which pass their own session) is unaffected —
only the ``python -m scripts.seed_*`` entrypoints call this.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

from app.config import get_settings

_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "db"}


def assert_local_database() -> None:
    url = get_settings().database_url
    # SQLAlchemy URLs with query-style host (?host=/tmp) are socket = local.
    if "host=/" in url:
        return
    host = (urlparse(url.replace("+asyncpg", "").replace("+psycopg2", "")).hostname
            or "")
    if host in _LOCAL_HOSTS:
        return
    if os.environ.get("ALLOW_REMOTE_SEED") == "true":
        print(f"⚠ ALLOW_REMOTE_SEED=true — seeding REMOTE database host {host!r}.")
        return
    sys.exit(
        f"REFUSING to seed: DATABASE_URL points at non-local host {host!r}.\n"
        "Seed scripts write synthetic users/data into shared core tables and "
        "must never run against the production database.\n"
        "If this is intentionally a disposable remote test DB, re-run with "
        "ALLOW_REMOTE_SEED=true."
    )

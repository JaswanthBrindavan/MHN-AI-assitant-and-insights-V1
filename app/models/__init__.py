"""ORM models package.

Importing this package must register every table on ``Base.metadata`` so
Alembic autogenerate and the test-time ``create_all`` see the full schema.
Model modules are added per phase; import them here as they land.
"""

from __future__ import annotations

from app.db import Base

__all__ = ["Base"]

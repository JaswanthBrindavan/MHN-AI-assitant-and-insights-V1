"""Background job run bookkeeping."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import CreatedAt, UUIDPrimaryKey


class JobRun(Base, UUIDPrimaryKey, CreatedAt):
    __tablename__ = "job_runs"

    name: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

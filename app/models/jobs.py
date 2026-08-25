"""Background job run bookkeeping."""

from __future__ import annotations

import uuid
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
    # WHO caused this job. Without it you learn that a document was read and
    # never by whom — the one field an access-control audit exists for.
    #
    # Nullable because scheduled work has no actor: a nightly sweep is caused
    # by the clock. A NULL therefore means "system", and must not be read as
    # "unknown user".
    #
    # Plain uuid, NO foreign key to "user" — the coexistence rule for Davi
    # tables, and necessary here besides: this row must outlive the account it
    # attributes, or an erasure would quietly erase the evidence of access.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, nullable=True, index=True
    )

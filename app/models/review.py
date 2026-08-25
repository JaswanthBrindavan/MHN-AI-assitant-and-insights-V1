"""Clinician review: who may review, and what they did.

drawbacks.md §8.7: `held_for_review` artifacts are generated today and seen by
NOBODY, EVER. The engine marks an insight sensitive, the read endpoint filters
it out, and there the matter ends — the most careful output the pipeline
produces is the one nothing surfaces.

This is the most security-sensitive surface in the repo, because it is the
only one where a person reads ANOTHER person's health information. Three
things follow from that, and all three are enforced in code:

1. **Membership is explicit and Davi-owned.** There is no role claim in the
   production JWT (``sub`` is a user UUID and nothing else), and we do not
   control what mhn-spring mints. So the roster lives here, as rows an
   administrator adds deliberately. Nobody is a clinician by default and
   nobody becomes one by holding a token.

2. **Every READ is audited, not just every decision.** A clinician opening a
   patient's sensitive insight is itself the event worth recording — by the
   time a decision exists, the information has already been seen.

3. **Revocation is immediate.** ``active=False`` ends access on the next
   request. The audit rows stay: they are the record of what happened while
   access was granted, and deleting them would defeat the point.

One caveat this table cannot cover, recorded so it is not discovered the hard
way: with ``SERVICE_TOKEN`` configured, ``app/auth.py`` accepts a valid service
token plus ``X-User-Id`` as identity without further checks. Anyone holding
that token can therefore present as a reviewer, and the audit trail will
faithfully record the *reviewer* as the accessor. The roster is the only thing
standing between an ordinary USER and cross-user access; it is not a defence
against a leaked service token. Treat that token accordingly.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import CreatedAt, UUIDPrimaryKey

# What a reviewer can decide about a held artifact.
DECISIONS = ("release", "suppress")

# Recorded actions. "view" is here because seeing the content IS the
# disclosure; a queue listing (title and condition only) is "list", filed
# against each patient it named. "audit_read" covers reading somebody else's
# trail, which is itself a cross-patient read.
ACTIONS = ("list", "view", "release", "suppress", "audit_read")


class ClinicianReviewer(Base, UUIDPrimaryKey, CreatedAt):
    """Someone permitted to review held insights.

    A row here is a grant of cross-user read access to sensitive health
    information. It should be created deliberately and rarely.
    """

    __tablename__ = "clinician_reviewers"
    __table_args__ = (
        sa.UniqueConstraint("user_id", name="uq_clinician_reviewer_user"),
    )

    # Plain uuid, NO FK to "user" — the coexistence rule for Davi tables.
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    # Free text for the human record: name, registration number, employer.
    # Never used for authorization — only user_id is.
    display_name: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    # Revocation flag rather than a delete: the audit rows reference this
    # reviewer, and the grant's history is part of the record.
    active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.text("true")
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class InsightReviewAudit(Base, UUIDPrimaryKey, CreatedAt):
    """An append-only record of clinician access and decisions.

    Append-only by discipline, not by constraint: nothing in the codebase
    updates or deletes a row here, and nothing should. If a decision was
    wrong, the correction is a NEW row, so the sequence stays readable.
    """

    __tablename__ = "insight_review_audit"

    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, nullable=False, index=True
    )
    # Whose information was involved. Indexed because "who looked at my
    # records?" is a question a patient is entitled to have answered.
    subject_user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, nullable=False, index=True
    )
    # Null for a "list" action, which spans artifacts rather than naming one.
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    action: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # Why a decision was made. Required for a decision, absent for a read.
    note: Mapped[str | None] = mapped_column(sa.String(1000), nullable=True)
    # The artifact's content hash AT THE TIME of the decision. An insight can
    # be recomputed from changed inputs; without this, a release recorded here
    # could not be tied to the text that was actually released.
    content_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

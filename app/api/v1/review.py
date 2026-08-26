"""Clinician review queue for held insights.

Every endpoint here reads or acts on ANOTHER person's health information, so
each one begins with the same two steps: confirm the caller is an active
reviewer, and write an audit row. The audit write happens BEFORE the content is
returned, not after, so a crash in between is not an unlogged disclosure.

The single exception, stated so nobody has to discover it: a patient reading
their OWN trail via `GET /review/audit?subject_user_id=<self>` needs no
reviewer standing, and reading your own record is not a disclosure to audit.
Every other path through this module — including an unscoped or cross-patient
audit read — requires standing and writes a row.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user_id
from app.db import get_db
from app.models.review import ClinicianReviewer, InsightReviewAudit
from app.models.rules import InsightArtifact
from app.telemetry import review_actions

router = APIRouter(prefix="/review", tags=["review"])


class QueueItem(BaseModel):
    """One held insight. The BODY is deliberately absent.

    A queue listing should not disclose the content of every held insight to
    anyone who opens the page — that would make the audited `view` step
    meaningless. The body arrives only from the detail endpoint.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    condition_code: str
    tier: str
    title: str
    content_hash: str
    created_at: str


class ArtifactDetail(QueueItem):
    body: str
    facts_used: dict | None = None
    fired_rules: list | None = None


class DecisionIn(BaseModel):
    note: str = Field(min_length=1, max_length=1000)


class DecisionOut(BaseModel):
    id: uuid.UUID
    status: str
    decided_by: uuid.UUID


async def _require_reviewer(db: AsyncSession, user_id: uuid.UUID) -> ClinicianReviewer:
    """403 unless the caller is on the roster AND still active.

    Note the `active` check: revocation must take effect on the very next
    request, not whenever a token happens to expire.
    """
    reviewer = (
        await db.execute(
            select(ClinicianReviewer).where(
                ClinicianReviewer.user_id == user_id,
                ClinicianReviewer.active.is_(True),
            )
        )
    ).scalars().first()
    if reviewer is None:
        # Same message either way — whether a given user id is a clinician is
        # not something an arbitrary caller should be able to probe.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorised for clinical review",
        )
    return reviewer


async def _audit(
    db: AsyncSession,
    *,
    reviewer: uuid.UUID,
    subject: uuid.UUID,
    action: str,
    artifact_id: uuid.UUID | None = None,
    note: str | None = None,
    content_hash: str | None = None,
) -> None:
    db.add(
        InsightReviewAudit(
            reviewer_user_id=reviewer,
            subject_user_id=subject,
            artifact_id=artifact_id,
            action=action,
            note=note,
            content_hash=content_hash,
        )
    )
    await db.flush()
    review_actions.inc(action=action)


@router.get("/queue", response_model=list[QueueItem])
async def review_queue(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    # Bounded at BOTH ends. `min(limit, 200)` alone clamps only the top: a
    # negative limit reaches SQLite as `LIMIT -1`, which means NO limit, and
    # the caller gets every held insight for every patient in one response.
    limit: int = Query(default=50, ge=1, le=200),
) -> list[QueueItem]:
    """Held insights awaiting a decision, oldest first.

    Oldest first on purpose: a queue sorted newest-first buries the item that
    has been waiting longest, which is the one most likely to matter.
    """
    await _require_reviewer(db, current_user)

    rows = (
        await db.execute(
            select(InsightArtifact)
            .where(InsightArtifact.status == "held_for_review")
            .order_by(InsightArtifact.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()

    # One row per DISTINCT patient in the page, not one row for the reviewer.
    #
    # A listing carries `user_id`, `condition_code` and a title like "Family
    # history of type 2 diabetes" — that is a patient bound to a named
    # condition, which is a disclosure. Filing it under the reviewer's own id
    # would keep it out of the patient's answer to "who looked at my records?",
    # which is the question this audit exists to answer. Bounded by the page
    # size, so it cannot flood the table.
    for subject in dict.fromkeys(r.user_id for r in rows):
        await _audit(db, reviewer=current_user, subject=subject, action="list")
    await db.commit()

    return [
        QueueItem(
            id=r.id,
            user_id=r.user_id,
            condition_code=r.condition_code,
            tier=r.tier,
            title=r.title,
            content_hash=r.content_hash,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]


@router.get("/artifacts/{artifact_id}", response_model=ArtifactDetail)
async def view_artifact(
    artifact_id: uuid.UUID,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ArtifactDetail:
    """The full held insight, including the facts it was derived from.

    A reviewer cannot judge whether an insight should be released without
    seeing what produced it — `facts_used` and `fired_rules` are the whole
    basis of the decision.
    """
    reviewer = await _require_reviewer(db, current_user)
    assert reviewer is not None  # _require_reviewer raises otherwise

    artifact = (
        await db.execute(
            select(InsightArtifact).where(InsightArtifact.id == artifact_id)
        )
    ).scalars().first()
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if artifact.status != "held_for_review":
        # Review access covers held artifacts ONLY. It is not a general
        # licence to read anybody's insights.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="That artifact is not awaiting review",
        )

    # Audited BEFORE the content is returned. Auditing after would leave a
    # crash between the read and the write as an unlogged disclosure.
    await _audit(
        db,
        reviewer=current_user,
        subject=artifact.user_id,
        action="view",
        artifact_id=artifact.id,
        content_hash=artifact.content_hash,
    )
    await db.commit()

    return ArtifactDetail(
        id=artifact.id,
        user_id=artifact.user_id,
        condition_code=artifact.condition_code,
        tier=artifact.tier,
        title=artifact.title,
        content_hash=artifact.content_hash,
        created_at=artifact.created_at.isoformat(),
        body=artifact.body,
        facts_used=artifact.facts_used,
        fired_rules=artifact.fired_rules,
    )


async def _decide(
    db: AsyncSession,
    artifact_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    action: str,
    note: str,
) -> DecisionOut:
    await _require_reviewer(db, reviewer_id)

    artifact = (
        await db.execute(
            select(InsightArtifact).where(InsightArtifact.id == artifact_id)
        )
    ).scalars().first()
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if artifact.user_id == reviewer_id:
        # Independent review is the entire premise of this table. A clinician
        # who is also the patient must not release or suppress their own held
        # insight — the audit row would show reviewer and subject as the same
        # person, which is a record of the control not working.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A reviewer cannot decide on their own insight",
        )
    if artifact.status != "held_for_review":
        # Deciding twice must not be possible. The second decision would
        # overwrite the first with no trace in the artifact itself.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already decided (status: {artifact.status})",
        )

    artifact.status = "active" if action == "release" else "suppressed"

    await _audit(
        db,
        reviewer=reviewer_id,
        subject=artifact.user_id,
        action=action,
        artifact_id=artifact.id,
        note=note,
        content_hash=artifact.content_hash,
    )
    await db.commit()
    return DecisionOut(id=artifact.id, status=artifact.status, decided_by=reviewer_id)


@router.post("/artifacts/{artifact_id}/release", response_model=DecisionOut)
async def release(
    artifact_id: uuid.UUID,
    payload: DecisionIn,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> DecisionOut:
    """Release a held insight to the patient.

    The note is REQUIRED. A decision with no stated reason is not reviewable
    later, and "it was approved" is not a record of why.
    """
    return await _decide(db, artifact_id, current_user, "release", payload.note)


@router.post("/artifacts/{artifact_id}/suppress", response_model=DecisionOut)
async def suppress(
    artifact_id: uuid.UUID,
    payload: DecisionIn,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> DecisionOut:
    """Withhold a held insight. It is never shown, and never re-queued.

    Suppressed is a LIVE status in the engine (see `LIVE_STATUSES`), which is
    what stops the next recompute from raising the same insight again for the
    same reviewer to decline a second time.
    """
    return await _decide(db, artifact_id, current_user, "suppress", payload.note)


@router.get("/audit")
async def audit_trail(
    subject_user_id: uuid.UUID | None = None,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """Who accessed what.

    Two callers are permitted, and only these two: a reviewer inspecting the
    trail, and a PATIENT asking who looked at their own records. The second is
    the one that makes the audit meaningful to the person it protects.
    """
    own_records = subject_user_id is not None and subject_user_id == current_user
    if not own_records:
        # Anything wider than "my own trail" requires reviewer standing — and
        # is itself a cross-patient read, so it is audited like any other. An
        # unscoped call returns the whole trail; a reviewer under investigation
        # must not be able to read the investigation surface invisibly.
        await _require_reviewer(db, current_user)
    subject = subject_user_id

    stmt = select(InsightReviewAudit).order_by(InsightReviewAudit.created_at.desc())
    if subject is not None:
        stmt = stmt.where(InsightReviewAudit.subject_user_id == subject)
    rows = (await db.execute(stmt.limit(limit))).scalars().all()

    if not own_records:
        await _audit(
            db,
            reviewer=current_user,
            subject=subject if subject is not None else current_user,
            action="audit_read",
        )
        await db.commit()

    return [
        {
            "id": str(r.id),
            "reviewer_user_id": str(r.reviewer_user_id),
            "subject_user_id": str(r.subject_user_id),
            "artifact_id": str(r.artifact_id) if r.artifact_id else None,
            "action": r.action,
            # The clinician's reason is written for clinicians. "Patient is
            # highly anxious, this will spiral them" is a defensible note and
            # an indefensible thing to hand the patient unannounced. They see
            # THAT a decision was made and when; the reasoning stays
            # professional correspondence.
            "note": r.note if not own_records else None,
            "content_hash": r.content_hash,
            "at": r.created_at.isoformat(),
        }
        for r in rows
    ]

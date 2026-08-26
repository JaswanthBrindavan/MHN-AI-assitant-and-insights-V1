"""Request, cancel and execute a full erasure.

`forget_everything` deleted three of the eleven Davi-owned per-user tables.
Episodes, insights, pedigree, sessions, messages, summaries and receipts all
survived a "forget me". This module replaces it with a complete, deferred
erasure.

DEFERRED, not delayed: `is_pending` suppresses every per-user memory read from
the moment the request is made, so the assistant forgets the reader
immediately even though the rows are destroyed later. See
`app/models/erasure.py` for why the window exists at all.

ORDER MATTERS. `insight_artifacts.superseded_by` is a self-referencing foreign
key, so the rows must be unlinked before they can be deleted; `user_profiles`
and `pedigree_conditions` reference `consent_ledger`, which is deliberately
kept, so they must go first. Getting this wrong surfaces as a foreign-key
violation halfway through a destructive operation, which is the worst moment
to discover it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import (
    ActiveSymptomState,
    ConversationSession,
    RagTurnReceipt,
    SymptomLog,
    UserMemory,
)
from app.models.common import utcnow
from app.models.core import PedigreeCondition, PedigreeMember
from app.models.erasure import CANCELLED, COMPLETED, PENDING, ErasureRequest
from app.models.feedback import TurnFeedback
from app.models.memory_document import UserMemoryDocument
from app.models.profile import UserProfile
from app.models.review import ClinicianReviewer
from app.models.rules import InsightArtifact

logger = logging.getLogger("davi.erasure")

# Deleted in this order. `conversation_sessions` last of the cascade parents so
# its ON DELETE CASCADE takes conversation_messages and conversation_summaries
# with it in one statement.
_ERASE_IN_ORDER = (
    ("user_profiles", UserProfile),
    ("pedigree_conditions", PedigreeCondition),
    ("pedigree_members", PedigreeMember),
    ("active_symptom_states", ActiveSymptomState),
    ("symptom_logs", SymptomLog),
    ("user_memories", UserMemory),
    ("turn_feedback", TurnFeedback),
    ("rag_turn_receipts", RagTurnReceipt),
    ("insight_artifacts", InsightArtifact),
    ("conversation_sessions", ConversationSession),
    # Derived, but it is a copy of the reader's data and must go with the
    # rest. Deleting it is also what stops a stale document being served
    # after the sources behind it are gone.
    ("user_memory_document", UserMemoryDocument),
)


async def request_erasure(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    grace_days: int,
    source: str = "api",
) -> ErasureRequest:
    """Schedule an erasure. Idempotent — an existing pending request is returned.

    Idempotent on purpose: someone anxious enough to ask twice should not end
    up with two windows, and the second request must not silently extend the
    first one's deadline.
    """
    existing = await pending_request(db, user_id)
    if existing is not None:
        return existing

    now = utcnow()
    request = ErasureRequest(
        user_id=user_id,
        requested_at=now,
        scheduled_for=now + timedelta(days=grace_days),
        status=PENDING,
        source=source,
    )
    db.add(request)
    await db.flush()

    # Any [P] block cached earlier in THIS session was built from data the
    # reader has now asked to stop being used. Local import: context.py imports
    # is_pending from here, so a module-level import would be a cycle.
    from app.chat.context import clear_patient_context_memo

    clear_patient_context_memo(db)
    # No user id, no free text: this log line is operational, and the erasure
    # path is the last place to start writing identifiers into logs.
    logger.info("erasure scheduled in %d days", grace_days)
    return request


async def pending_request(
    db: AsyncSession, user_id: uuid.UUID
) -> ErasureRequest | None:
    return (
        await db.execute(
            select(ErasureRequest).where(
                ErasureRequest.user_id == user_id,
                ErasureRequest.status == PENDING,
            )
        )
    ).scalars().first()


async def is_pending(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """True while an erasure is scheduled but not yet executed.

    Callers use this to stop USING the data immediately. Fails CLOSED: if the
    check itself errors we behave as though an erasure is pending and withhold
    the memory, because wrongly remembering someone who asked to be forgotten
    is the worse of the two mistakes.
    """
    try:
        return await pending_request(db, user_id) is not None
    except Exception:  # noqa: BLE001
        logger.warning("erasure-pending check failed; withholding memory")
        return True


async def cancel_erasure(
    db: AsyncSession, user_id: uuid.UUID
) -> ErasureRequest | None:
    """Withdraw a pending request. The grace period exists for this."""
    request = await pending_request(db, user_id)
    if request is None:
        return None
    request.status = CANCELLED
    request.cancelled_at = utcnow()
    await db.flush()
    return request


async def purge_user(db: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """Destroy every Davi-owned per-user row. Returns per-table counts.

    NOT called directly by the API — erasure goes through the scheduled path.
    Exposed for the sweep and for an operator who needs to execute one
    immediately with a written reason.
    """
    counts: dict[str, int] = {}

    # Unlink the self-referencing FK before deleting, or the delete order
    # inside the table matters and Postgres will reject it.
    await db.execute(
        update(InsightArtifact)
        .where(InsightArtifact.user_id == user_id)
        .values(superseded_by=None)
    )

    for label, model in _ERASE_IN_ORDER:
        result = await db.execute(
            delete(model).where(model.user_id == user_id)
        )
        counts[label] = getattr(result, "rowcount", 0) or 0

    # A reviewer grant is a ROLE, not health data. Revoke rather than delete:
    # insight_review_audit references this reviewer, and that audit must
    # outlive them or past access becomes unaccountable.
    revoked = await db.execute(
        update(ClinicianReviewer)
        .where(ClinicianReviewer.user_id == user_id)
        .values(active=False, revoked_at=utcnow())
    )
    counts["clinician_reviewers_revoked"] = getattr(revoked, "rowcount", 0) or 0

    await db.flush()
    return counts


async def execute_due(
    db: AsyncSession, now: datetime | None = None, limit: int = 500
) -> dict:
    """Execute every erasure whose grace period has expired.

    Batched and committed per user: a failure on one account must not roll back
    the erasures that already succeeded, and must not leave one half-erased.
    """
    now = now or utcnow()
    due = (
        await db.execute(
            select(ErasureRequest)
            .where(
                ErasureRequest.status == PENDING,
                ErasureRequest.scheduled_for <= now,
            )
            .order_by(ErasureRequest.scheduled_for)
            .limit(limit)
        )
    ).scalars().all()

    executed, failed = 0, 0
    for request in due:
        try:
            counts = await purge_user(db, request.user_id)
            request.status = COMPLETED
            request.completed_at = utcnow()
            request.deleted_counts = counts
            await db.commit()
            executed += 1
        except Exception:  # noqa: BLE001 — one bad account must not stop the rest
            await db.rollback()
            logger.warning("erasure failed for one account", exc_info=True)
            failed += 1

    return {"erasures_executed": executed, "erasures_failed": failed}

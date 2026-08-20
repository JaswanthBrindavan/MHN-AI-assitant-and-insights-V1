"""Submit chat-uploaded documents to the mhn-ai pipeline.

Davi does NOTHING with documents itself — no file bytes, no document rows.
When a user shares a file in chat, the upload runs through Spring's existing
flow (S3 + the ``unclassified_files`` row), exactly as everywhere else in the
product; Davi then submits the processing run to mhn-ai the same way Spring
does. Contract verified against the mhn-ai repo (``app/api/v1/runs.py``,
``app/schemas/runs.py``, ``app/api/deps.py``):

    POST {MHN_AI_BASE_URL}/v1/document-processing-runs
    Authorization: Bearer <MHN_SERVICE_TOKEN>        (mhn-ai's service token)
    {"documents": [{"document_id": <unclassified_files id>,
                    "intended_section": null}],
     "requested_by_user_id": "<uuid>"}               → 202 {run_id, items[]}

Davi only READS ``unclassified_files`` (to check the document belongs to the
caller before submitting). Fail-open: if mhn-ai is unreachable or
unconfigured, the chat turn still succeeds and the document stays
unprocessed (retryable); a ``job_runs`` row records what happened.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.common import utcnow
from app.models.coredata import UnclassifiedFile
from app.models.jobs import JobRun

logger = logging.getLogger("davi.documents")

UPLOAD_RESOURCE_TYPE = "unclassified_files"
_RUNS_PATH = "/v1/document-processing-runs"


@dataclass(frozen=True)
class TriggerResult:
    accepted: bool
    run_id: str | None = None
    item_status: str | None = None  # "queued" | "failed" (publish_failed) | …


async def get_own_unclassified(
    db: AsyncSession, user_id: uuid.UUID, document_id: int
) -> UnclassifiedFile | None:
    """The row, only if this user is its subject or its uploader (READ only —
    the row itself is created by Spring's upload flow, never by Davi)."""
    row = (
        await db.execute(
            select(UnclassifiedFile).where(UnclassifiedFile.id == document_id)
        )
    ).scalars().first()
    if row is None:
        return None
    if row.user_id != user_id and row.created_by != user_id:
        return None
    return row


async def trigger_mhn_ai(
    document_id: int,
    user_id: uuid.UUID,
    client: httpx.AsyncClient | None = None,
) -> TriggerResult:
    """Submit the document to mhn-ai's processing-run API. Never raises.

    Not-accepted results leave the document unprocessed but retryable — the
    chat turn must succeed regardless.
    """
    settings = get_settings()
    if not settings.mhn_ai_base_url:
        return TriggerResult(accepted=False)
    url = settings.mhn_ai_base_url.rstrip("/") + _RUNS_PATH
    headers = {"X-Request-Id": uuid.uuid4().hex}
    if settings.mhn_ai_token:
        headers["Authorization"] = f"Bearer {settings.mhn_ai_token}"
    payload = {
        "documents": [
            {"document_id": document_id, "intended_section": None}
        ],
        # Audit only — mhn-ai performs no user-level authorization.
        "requested_by_user_id": str(user_id),
    }
    try:
        if client is not None:
            resp = await client.post(url, json=payload, headers=headers)
        else:
            async with httpx.AsyncClient(
                timeout=settings.mhn_ai_timeout_seconds
            ) as c:
                resp = await c.post(url, json=payload, headers=headers)
        if not (200 <= resp.status_code < 300):
            logger.warning(
                "mhn-ai run submission rejected: HTTP %s for doc %s",
                resp.status_code, document_id,
            )
            return TriggerResult(accepted=False)
        run_id: str | None = None
        item_status: str | None = None
        try:
            body = resp.json()
            run_id = str(body.get("run_id")) if body.get("run_id") else None
            items = body.get("items") or []
            if items:
                item_status = items[0].get("status")
        except Exception:  # noqa: BLE001 — the 202 already means accepted
            pass
        return TriggerResult(
            accepted=True, run_id=run_id, item_status=item_status
        )
    except Exception:  # noqa: BLE001 — the chat turn must succeed regardless
        logger.warning(
            "mhn-ai run submission failed for doc %s", document_id,
            exc_info=True,
        )
        return TriggerResult(accepted=False)


async def submit_document(
    db: AsyncSession,
    user_id: uuid.UUID,
    document_id: int,
    client: httpx.AsyncClient | None = None,
) -> TriggerResult:
    """Trigger mhn-ai for an existing document, with job bookkeeping."""
    job = JobRun(
        name="chat_upload_trigger",
        trigger="chat",
        status="running",
        started_at=utcnow(),
    )
    db.add(job)
    result = await trigger_mhn_ai(document_id, user_id, client=client)
    job.status = "success" if result.accepted else "trigger_failed"
    job.finished_at = utcnow()
    return result


def build_upload_reply(filename: str, triggered: bool) -> str:
    """Deterministic, validator-safe confirmation for the chat transcript."""
    if triggered:
        return (
            f"Got your file '{filename}' — it has been sent for automatic "
            "classification. The extracted details will appear in your "
            "records shortly, and you can ask me about it anytime — for "
            "example \"find my latest report\"."
        )
    return (
        f"Your file '{filename}' is uploaded, but automatic classification "
        "could not be started right now. It stays safely in your records "
        "and can be processed later."
    )

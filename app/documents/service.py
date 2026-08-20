"""Chat uploads: store the document, then trigger the mhn-ai pipeline.

Classification/extraction is NOT done here — that is mhn-ai's job (classify →
file → extract). Contract verified against the mhn-ai repo
(``app/api/v1/runs.py``, ``app/schemas/runs.py``, ``app/api/deps.py``):

    POST {MHN_AI_BASE_URL}/v1/document-processing-runs
    Authorization: Bearer <MHN_SERVICE_TOKEN>        (mhn-ai's service token)
    {"documents": [{"document_id": <unclassified_files id>,
                    "intended_section": null}],
     "requested_by_user_id": "<uuid>"}               → 202 {run_id, items[]}

The submitted unit is an ``unclassified_files`` row. Davi's chat upload
inserts that row itself (dev/chassis: bytes under UPLOAD_DIR as a stand-in
for the S3 key the worker downloads); in the full production flow the file
reaches S3 + ``unclassified_files`` via Spring, and the caller passes the
existing ``document_id`` instead of bytes.

Fail-open: if mhn-ai is unreachable or unconfigured, the upload still
succeeds and the document simply stays unprocessed (retryable later); a
``job_runs`` row records what happened.
"""

from __future__ import annotations

import hashlib
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


@dataclass(frozen=True)
class StoredUpload:
    resource_type: str
    doc_id: int
    triggered: bool
    run_id: str | None = None
    item_status: str | None = None


async def store_document(
    db: AsyncSession,
    user_id: uuid.UUID,
    filepath: str,
    name: str | None = None,
) -> UnclassifiedFile:
    """Insert the uploaded document row awaiting classification."""
    row = UnclassifiedFile(
        user_id=user_id,
        filepath=filepath,
        name=(name or None) and name[:255],
        private=False,
        created_by=user_id,
        created_at=utcnow(),
    )
    db.add(row)
    await db.flush()
    return row


async def get_own_unclassified(
    db: AsyncSession, user_id: uuid.UUID, document_id: int
) -> UnclassifiedFile | None:
    """The row, only if this user is its subject or its uploader."""
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
    upload itself must succeed regardless.
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
    except Exception:  # noqa: BLE001 — the upload must succeed regardless
        logger.warning(
            "mhn-ai run submission failed for doc %s", document_id,
            exc_info=True,
        )
        return TriggerResult(accepted=False)


async def store_and_trigger(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: bytes,
    filepath: str,
    name: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> StoredUpload:
    """Store the upload and submit it to mhn-ai, with job bookkeeping."""
    job = JobRun(
        name="chat_upload_trigger",
        trigger="chat",
        status="running",
        started_at=utcnow(),
        input_hash=hashlib.sha256(data).hexdigest(),
    )
    db.add(job)
    try:
        row = await store_document(db, user_id, filepath, name=name)
    except Exception as exc:  # noqa: BLE001 — record, then surface
        job.status = "failed"
        job.finished_at = utcnow()
        job.error = str(exc)[:500]
        raise
    result = await trigger_mhn_ai(row.id, user_id, client=client)
    job.status = "success" if result.accepted else "stored_not_triggered"
    job.finished_at = utcnow()
    return StoredUpload(
        resource_type=UPLOAD_RESOURCE_TYPE,
        doc_id=row.id,
        triggered=result.accepted,
        run_id=result.run_id,
        item_status=result.item_status,
    )


def build_upload_reply(filename: str, triggered: bool) -> str:
    """Deterministic, validator-safe confirmation for the chat transcript."""
    if triggered:
        return (
            f"Stored your file '{filename}' and sent it for automatic "
            "classification. The extracted details will appear in your "
            "records shortly — you can ask me about it anytime, for example "
            "\"find my latest report\"."
        )
    return (
        f"Stored your file '{filename}'. Automatic classification could not "
        "be started right now, so the file is saved and will be processed "
        "later. You can still ask me about your records anytime."
    )

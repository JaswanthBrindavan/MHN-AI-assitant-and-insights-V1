"""Chat uploads: store the document, then trigger the mhn-ai pipeline.

Classification/extraction is NOT done here — that is mhn-ai's job (classify →
file → extract, writing the ``content.ai`` envelope). This service stores the
uploaded document in state ``pending`` and pokes mhn-ai's API over the same
bearer-token server-to-server pattern Spring uses (``AI_TOKEN``). Fail-open:
if mhn-ai is unreachable or unconfigured, the upload still succeeds and the
document simply stays pending; a ``job_runs`` row records what happened.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.common import utcnow
from app.models.coredata import Report
from app.models.jobs import JobRun

logger = logging.getLogger("davi.documents")

# New uploads land in `reports` in state "pending"; mhn-ai's filing step owns
# any reclassification/relocation, exactly as in the Spring-fed flow.
UPLOAD_RESOURCE_TYPE = "reports"


@dataclass(frozen=True)
class StoredUpload:
    resource_type: str
    doc_id: int
    triggered: bool  # True when mhn-ai accepted the processing request


async def store_document(
    db: AsyncSession, user_id: uuid.UUID, filepath: str
) -> Report:
    """Insert the uploaded document row (state ``pending``, awaiting mhn-ai)."""
    row = Report(
        user_id=user_id,
        filepath=filepath,
        content={
            "ai": {
                "schema_version": "2.1",
                "state": "pending",
                "source": "davi_chat_upload",
            }
        },
        private=False,
        created_at=utcnow(),
    )
    db.add(row)
    await db.flush()
    return row


async def trigger_mhn_ai(
    resource_type: str,
    doc_id: int,
    user_id: uuid.UUID,
    filepath: str,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """POST the document to mhn-ai's processing API. Never raises.

    Returns True when mhn-ai accepted the request (2xx). False when the
    trigger is disabled (no base URL) or the call failed — the document stays
    ``pending`` either way and can be re-processed later.
    """
    settings = get_settings()
    if not settings.mhn_ai_base_url:
        return False
    url = settings.mhn_ai_base_url.rstrip("/") + settings.mhn_ai_process_path
    headers = {}
    if settings.mhn_ai_token:
        headers["Authorization"] = f"Bearer {settings.mhn_ai_token}"
    payload = {
        "resource_type": resource_type,
        "document_id": doc_id,
        "user_id": str(user_id),
        "filepath": filepath,
    }
    try:
        if client is not None:
            resp = await client.post(url, json=payload, headers=headers)
        else:
            async with httpx.AsyncClient(
                timeout=settings.mhn_ai_timeout_seconds
            ) as c:
                resp = await c.post(url, json=payload, headers=headers)
        if 200 <= resp.status_code < 300:
            return True
        logger.warning(
            "mhn-ai trigger rejected: HTTP %s for doc %s",
            resp.status_code, doc_id,
        )
        return False
    except Exception:  # noqa: BLE001 — the upload must succeed regardless
        logger.warning("mhn-ai trigger failed for doc %s", doc_id,
                       exc_info=True)
        return False


async def store_and_trigger(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: bytes,
    filepath: str,
    client: httpx.AsyncClient | None = None,
) -> StoredUpload:
    """Store the upload and hand it to mhn-ai, with job bookkeeping."""
    job = JobRun(
        name="chat_upload_trigger",
        trigger="chat",
        status="running",
        started_at=utcnow(),
        input_hash=hashlib.sha256(data).hexdigest(),
    )
    db.add(job)
    try:
        row = await store_document(db, user_id, filepath)
    except Exception as exc:  # noqa: BLE001 — record, then surface
        job.status = "failed"
        job.finished_at = utcnow()
        job.error = str(exc)[:500]
        raise
    triggered = await trigger_mhn_ai(
        UPLOAD_RESOURCE_TYPE, row.id, user_id, filepath, client=client
    )
    job.status = "success" if triggered else "stored_not_triggered"
    job.finished_at = utcnow()
    return StoredUpload(
        resource_type=UPLOAD_RESOURCE_TYPE, doc_id=row.id, triggered=triggered
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

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


def _mhn_ai_base() -> str | None:
    """The configured mhn-ai base URL, normalized. None = not configured.

    Found live: the env var was set without a scheme
    ("mhn-ai.railway.internal:8000"), which httpx rejects with
    UnsupportedProtocol — silently breaking every trigger and result fetch.
    A missing scheme now defaults to http:// (Railway private networking).
    """
    raw = get_settings().mhn_ai_base_url.strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw.rstrip("/")


@dataclass(frozen=True)
class TriggerResult:
    accepted: bool
    run_id: str | None = None
    item_status: str | None = None  # "queued" | "failed" (publish_failed) | …
    # Why a submission was NOT accepted ("not_configured", "http_401",
    # "connect_error: …") — recorded in job_runs and surfaced to the client
    # so a misconfigured trigger is diagnosable without server logs.
    reason: str | None = None


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
    base = _mhn_ai_base()
    if base is None:
        return TriggerResult(accepted=False, reason="not_configured")
    url = base + _RUNS_PATH
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
            return TriggerResult(
                accepted=False, reason=f"http_{resp.status_code}"
            )
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
    except Exception as exc:  # noqa: BLE001 — the chat turn must succeed regardless
        logger.warning(
            "mhn-ai run submission failed for doc %s", document_id,
            exc_info=True,
        )
        return TriggerResult(
            accepted=False,
            reason=f"{type(exc).__name__}: {str(exc)[:120]}",
        )


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
    if not result.accepted:
        job.error = result.reason
    job.finished_at = utcnow()
    return result


def build_upload_reply(filename: str) -> str:
    """Deterministic, validator-safe confirmation for the chat transcript.

    Trigger outcome deliberately does not change the wording: Spring submits
    every confirmed upload to the pipeline itself (AiSubmissionListener), so
    Davi's own submission is a redundant belt-and-braces call — its failure
    does not mean processing isn't running. The reason still lands in
    job_runs and the response's trigger_reason for diagnostics.
    """
    return (
        f"Got your file '{filename}' — it's queued for automatic "
        "processing. The extracted details will appear in your records "
        "shortly; ask me for insights on it once it's done."
    )


# --------------------------------------------------------------------------- #
# Reading a document's AI result (insights / section extraction) from mhn-ai
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AiResultFetch:
    ok: bool
    #: Lifecycle status of the latest processing item, when the status call
    #: succeeded ("completed", "classifying", "failed", …).
    status: str | None = None
    document_type: str | None = None
    #: The full DocumentAiResult JSON, when the typed result call succeeded.
    result: dict | None = None
    reason: str | None = None
    #: The item's last_error_code ("name_mismatch", "publish_failed", …).
    error_code: str | None = None
    #: The status endpoint's name_check payload ({verdict, document_name,
    #: confirmed}) — a MISMATCHED document is never filed, and this is the
    #: only place chat can learn why it appears stuck.
    name_check: dict | None = None


async def fetch_ai_result(
    document_id: int,
    client: httpx.AsyncClient | None = None,
) -> AiResultFetch:
    """Pull a document's AI result from mhn-ai. Never raises.

    Two verified calls: ``GET /v1/documents/{id}/status`` names the type the
    result is readable under, then ``GET /v1/documents/{type}/{id}/ai-result``
    returns it — insights for reports, section extraction for other types.
    """
    settings = get_settings()
    base = _mhn_ai_base()
    if base is None:
        return AiResultFetch(ok=False, reason="not_configured")
    headers = {"X-Request-Id": uuid.uuid4().hex}
    if settings.mhn_ai_token:
        headers["Authorization"] = f"Bearer {settings.mhn_ai_token}"

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=settings.mhn_ai_timeout_seconds)
    try:
        status_resp = await client.get(
            f"{base}/v1/documents/{document_id}/status", headers=headers
        )
        if status_resp.status_code != 200:
            return AiResultFetch(
                ok=False, reason=f"status_http_{status_resp.status_code}"
            )
        status_body = status_resp.json()
        lifecycle = status_body.get("status")
        doc_type = status_body.get("document_type")
        error_code = status_body.get("last_error_code")
        name_check = status_body.get("name_check")
        if not isinstance(name_check, dict):
            name_check = None
        if not doc_type or error_code == "name_mismatch":
            # Not classified yet, a type with no addressable result, or held
            # on a name mismatch (the typed result route refuses those with
            # 409 — do not even ask).
            return AiResultFetch(
                ok=True, status=lifecycle, document_type=doc_type,
                error_code=error_code, name_check=name_check,
            )

        result_resp = await client.get(
            f"{base}/v1/documents/{doc_type}/{document_id}/ai-result",
            headers=headers,
        )
        if result_resp.status_code != 200:
            return AiResultFetch(
                ok=False, status=lifecycle, document_type=doc_type,
                reason=f"result_http_{result_resp.status_code}",
                error_code=error_code, name_check=name_check,
            )
        return AiResultFetch(
            ok=True, status=lifecycle, document_type=doc_type,
            result=result_resp.json(),
            error_code=error_code, name_check=name_check,
        )
    except Exception as exc:  # noqa: BLE001 — the chat turn must succeed regardless
        logger.warning(
            "mhn-ai ai-result fetch failed for doc %s", document_id,
            exc_info=True,
        )
        return AiResultFetch(
            ok=False, reason=f"{type(exc).__name__}: {str(exc)[:120]}"
        )
    finally:
        if own_client:
            await client.aclose()

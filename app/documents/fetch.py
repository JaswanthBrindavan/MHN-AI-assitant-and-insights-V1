"""Fetching document bytes — the narrow, audited exception to "Davi holds no S3".

Davi still holds **no AWS credentials**. That posture is deliberate and is not
being abandoned here: the blast radius of this service stays the database
permissions it runs with. What changes is that Davi may now ask *Spring* — which
already owns the bucket and already authorizes file reads — to mint a short-lived
presigned GET, and then read those bytes into memory for a single turn.

Three guards, in this order:

1. **Davi checks consent itself**, using the same four-condition gate as every
   other family read (accepted connection + owner-side grant + not private + no
   per-file exclusion). This is defence in depth: Spring re-checks when it mints
   the URL, and a bug in either alone cannot widen access.
2. **Spring mints the URL.** Davi never signs anything and never sees a key.
3. **The bytes are bounded and never persisted.** Size cap, content-type
   allowlist, timeout, in-memory only. Nothing is written to disk or to a table.

Every fetch writes a ``job_runs`` row, so "which documents did the AI read" is
answerable from the database rather than from logs.

Fail-closed throughout: any doubt returns None and the caller falls back to the
extracted ``content.ai`` it has always used.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.coredata.service import _RESOURCE_TYPE, can_view_document
from app.models.common import utcnow
from app.models.jobs import JobRun

logger = logging.getLogger("davi.documents")


class HttpGetter(Protocol):
    """The only thing this module needs from an HTTP client.

    Narrower than httpx.AsyncClient on purpose: it states the actual
    requirement, and it lets a test supply a stub without pretending to be a
    full client.
    """

    async def get(self, url: str, **kwargs: Any) -> Any: ...

    def stream(self, method: str, url: str, **kwargs: Any) -> Any: ...


# What a vision model can actually read. A PDF is deliberately NOT here: the
# extraction pipeline already turns those into content.ai, and re-reading the
# raw file would be duplicating mhn-ai's job with a worse tool.
ALLOWED_CONTENT_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
)

# Hard ceiling on what is pulled into memory for one turn, enforced
# INCREMENTALLY as the body streams in. Larger than any phone photo, far
# smaller than anything that would threaten the process.
MAX_BYTES = 12 * 1024 * 1024

_URL_PATH = "/files/{resource_type}/{resource_id}/url"


@dataclass(frozen=True)
class FetchedDocument:
    """Bytes for one document, held only for the duration of a turn."""

    content: bytes
    content_type: str
    resource_type: str
    resource_id: int

    @property
    def size(self) -> int:
        return len(self.content)


def _spring_base() -> str | None:
    """The configured Spring base URL, normalized. None = feature off.

    Mirrors _mhn_ai_base: a missing scheme defaults to http://, because
    Railway private networking is http and an unschemed value otherwise fails
    with UnsupportedProtocol and silently disables the whole path.
    """
    raw = get_settings().mhn_spring_base_url.strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw.rstrip("/")


async def _record(
    db: AsyncSession, *, status: str, detail: str, resource: str
) -> None:
    """Audit row. Never raises — an audit failure must not break a reply, but
    it must also never silently skip the reply itself."""
    try:
        job = JobRun(
            name="document_fetch",
            trigger="chat",
            status=status,
            started_at=utcnow(),
            finished_at=utcnow(),
            error=detail if status != "success" else None,
            # The resource, never the bytes and never the content.
            input_hash=resource[:64],
        )
        db.add(job)
        await db.flush()
    except Exception:  # noqa: BLE001
        logger.warning("document fetch audit row failed", exc_info=True)


async def _presigned_url(
    resource_type: str,
    resource_id: int,
    viewer_id: uuid.UUID,
    client: HttpGetter | None = None,
) -> str | None:
    """Ask Spring for a presigned GET. Davi never mints one itself."""
    base = _spring_base()
    settings = get_settings()
    if not base or not settings.mhn_spring_token:
        return None

    url = base + _URL_PATH.format(
        resource_type=resource_type, resource_id=resource_id
    )
    headers = {
        "Authorization": f"Bearer {settings.mhn_spring_token}",
        # Spring authorizes the file read for THIS user, exactly as it does
        # for the app's own requests.
        "X-User-Id": str(viewer_id),
    }
    try:
        if client is not None:
            resp = await client.get(
                url,
                headers=headers,
                timeout=settings.mhn_spring_timeout_seconds,
            )
        else:
            async with httpx.AsyncClient(
                timeout=settings.mhn_spring_timeout_seconds
            ) as owned:
                resp = await owned.get(url, headers=headers)
        if resp.status_code != 200:
            logger.warning("spring file url -> HTTP %s", resp.status_code)
            return None
        data = resp.json()
    except Exception:  # noqa: BLE001 — fail closed
        logger.warning("spring file url request failed", exc_info=True)
        return None

    if not isinstance(data, dict):
        return None
    signed = data.get("url") or data.get("presignedUrl") or data.get("downloadUrl")
    if not isinstance(signed, str):
        return None
    # startswith("http") also accepts "httpfoo://". Be exact.
    return signed if signed.startswith(("https://", "http://")) else None


async def fetch_document_bytes(
    db: AsyncSession,
    *,
    viewer_id: uuid.UUID,
    owner_id: uuid.UUID,
    kind: str,
    resource_id: int,
    is_private: bool | None,
    client: HttpGetter | None = None,
) -> FetchedDocument | None:
    """Read one document's bytes, or None.

    None is returned — never an exception — for every refusal and every
    failure, so the caller simply falls back to the extracted content.ai it has
    always used.
    """
    resource_type = _RESOURCE_TYPE.get(kind, kind)
    resource = f"{resource_type}:{resource_id}"

    # 1. Davi's own consent check, BEFORE anything leaves the process.
    allowed = await can_view_document(
        db,
        viewer_id=viewer_id,
        owner_id=owner_id,
        resource_type=resource_type,
        resource_id=resource_id,
        is_private=is_private,
    )
    if not allowed:
        logger.warning("document fetch refused by consent gate")
        await _record(db, status="refused", detail="consent", resource=resource)
        return None

    if _spring_base() is None:
        await _record(
            db, status="skipped", detail="not_configured", resource=resource
        )
        return None

    # 2. Spring mints the URL — and re-checks authorization while doing so.
    signed = await _presigned_url(resource_type, resource_id, viewer_id, client)
    if signed is None:
        await _record(db, status="failed", detail="no_url", resource=resource)
        return None

    # 3. Bounded read, in memory only.
    #
    # STREAMED, deliberately. The first version did `body = resp.content` and
    # checked the size afterwards — but httpx buffers the whole response before
    # returning when stream=False, so the allocation had already happened. That
    # made MAX_BYTES a ceiling on what was RETURNED, not on what was read: a
    # hostile or broken upstream could still have exhausted memory before the
    # check ran. Streaming makes the cap real, and lets the content-type be
    # rejected from the headers before a single byte of body is read.
    settings = get_settings()
    try:
        if client is not None:
            stream_ctx = client.stream(
                "GET", signed, timeout=settings.mhn_spring_timeout_seconds
            )
            owned_client = None
        else:
            owned_client = httpx.AsyncClient(
                timeout=settings.mhn_spring_timeout_seconds
            )
            stream_ctx = owned_client.stream("GET", signed)

        try:
            async with stream_ctx as resp:
                if resp.status_code != 200:
                    await _record(
                        db, status="failed",
                        detail=f"http_{resp.status_code}", resource=resource,
                    )
                    return None

                content_type = (
                    resp.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                if content_type not in ALLOWED_CONTENT_TYPES:
                    await _record(
                        db, status="refused",
                        detail=f"type_{content_type[:64]}", resource=resource,
                    )
                    return None

                # Trust the declared length when it is already too big, then
                # verify as we read — a lying header must not get a free pass.
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                    await _record(
                        db, status="refused", detail="too_large", resource=resource
                    )
                    return None

                buffer = bytearray()
                async for chunk in resp.aiter_bytes():
                    buffer += chunk
                    if len(buffer) > MAX_BYTES:
                        await _record(
                            db, status="refused", detail="too_large",
                            resource=resource,
                        )
                        return None
                body = bytes(buffer)
        finally:
            if owned_client is not None:
                await owned_client.aclose()
    except Exception:  # noqa: BLE001 — fail closed
        logger.warning("document byte fetch failed", exc_info=True)
        await _record(db, status="failed", detail="transport", resource=resource)
        return None

    if not body:
        await _record(db, status="failed", detail="empty", resource=resource)
        return None

    await _record(db, status="success", detail="", resource=resource)
    return FetchedDocument(
        content=body,
        content_type=content_type,
        resource_type=resource_type,
        resource_id=resource_id,
    )

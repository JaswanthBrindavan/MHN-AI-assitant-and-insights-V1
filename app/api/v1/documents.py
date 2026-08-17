"""Dev-only document preview — renders extracted content for the test console.

In PRODUCTION this endpoint does not exist (404 unless APP_ENV=dev): clients
open the actual file via the existing app flow — Spring's presigned
``GET /files/{type}/{id}/url`` or the health-wallet detail routes. This
preview exists so the local console can demonstrate the open-a-document
experience without S3: it renders the document's ``content.ai`` extraction
(title, lab table with abnormal flags) behind the SAME consent gate production
enforces (owner / owner-side read grant / private / per-file exclusions).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user_id
from app.config import get_settings
from app.coredata.service import can_view_document
from app.db import get_db
from app.models.coredata import Prescription, Report, ScanImaging, Vaccination

router = APIRouter(tags=["documents"])

_MODELS = {
    "reports": Report,
    "scans_imaging": ScanImaging,
    "prescriptions": Prescription,
    "vaccinations": Vaccination,
}


def _esc(t: object) -> str:
    return (
        str(t)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _render(doc, resource_type: str) -> str:
    ai = (doc.content or {}).get("ai", {}) if isinstance(doc.content, dict) else {}
    title = (ai.get("classification") or {}).get("title") or \
        doc.filepath.rsplit("/", 1)[-1]
    when = doc.created_at.strftime("%d %b %Y") if doc.created_at else ""
    results = ((ai.get("extraction") or {}) or {}).get("results") or []

    rows = ""
    for r in results:
        flag = r.get("abnormal_flag") or ""
        color = {"high": "#e11d48", "low": "#d97706"}.get(flag, "#059669")
        rows += (
            f"<tr><td>{_esc(r.get('test_name', ''))}</td>"
            f"<td><b>{_esc(r.get('value', ''))} {_esc(r.get('unit') or '')}</b></td>"
            f"<td>{_esc(r.get('reference_range') or '—')}</td>"
            f"<td style='color:{color};font-weight:600'>{_esc(flag or 'normal')}</td></tr>"
        )
    table = (
        "<table><tr><th>Test</th><th>Value</th><th>Reference</th><th>Flag</th></tr>"
        + rows + "</table>"
        if rows else
        "<p class='muted'>No extracted values on this document.</p>"
    )
    return f"""<!doctype html><meta charset="utf-8">
<style>
 body {{ font: 14px/1.5 -apple-system, system-ui, sans-serif; margin: 20px;
        color: #111827; background: #fff; }}
 h2 {{ margin: 0 0 2px; }} .muted {{ color: #6b7280; font-size: 12px; }}
 table {{ border-collapse: collapse; margin-top: 12px; width: 100%; }}
 th, td {{ text-align: left; padding: 7px 12px; border-bottom: 1px solid #e5e7eb;
          font-size: 13px; }}
 th {{ color: #6b7280; font-weight: 600; font-size: 11px;
      text-transform: uppercase; }}
 .note {{ margin-top: 16px; padding: 10px 12px; background: #f3f4f6;
         border-radius: 8px; font-size: 12px; color: #4b5563; }}
</style>
<h2>{_esc(title)}</h2>
<div class="muted">{_esc(resource_type)} · {_esc(when)} · dev preview</div>
{table}
<div class="note">Dev-console preview rendered from the document's extracted
content (content.ai). In production the actual file opens via the app's
existing viewer (Spring presigned URL — Davi holds no AWS credentials).</div>
"""


@router.get("/documents/{resource_type}/{doc_id}/preview", response_class=HTMLResponse)
async def document_preview(
    resource_type: str,
    doc_id: int,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    if get_settings().app_env != "dev":
        raise HTTPException(status_code=404, detail="Not found")
    model = _MODELS.get(resource_type)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown document type")
    doc = (
        await db.execute(select(model).where(model.id == doc_id))  # type: ignore[attr-defined]
    ).scalars().first()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    allowed = await can_view_document(
        db, current_user, doc.user_id, resource_type, doc_id,
        is_private=doc.private,
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="You don't have access to this file.")
    return HTMLResponse(_render(doc, resource_type))

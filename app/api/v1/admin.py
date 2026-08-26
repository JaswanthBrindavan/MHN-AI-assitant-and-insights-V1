"""Service-to-service admin endpoints (staff dashboard → this service).

The R&D dashboard edits condition_registry rows (aliases, names, merges,
MCP uploads). Two things in this process must react or the chat never sees
the change until a restart:

  1. the cached keyword index (``app.knowledge.registry``) — rebuilt lazily
     after ``reset_index_cache()``;
  2. the condition's *alias card* in ``mcp_chunks`` — one small chunk that
     names every alias, embedded so vector retrieval can answer "what is
     <alias>" even when the phrasing never mentions the display name.

Auth: static SERVICE_TOKEN bearer only (the Spring↔mhn-ai pattern). User
JWTs are deliberately rejected — these endpoints are not user-facing.
"""

from __future__ import annotations

import hmac
import logging

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.knowledge.registry import reset_index_cache
from app.models.chat import McpChunk
from app.models.knowledge import ConditionRegistry
from app.rag.embeddings import embed_texts, embeddings_configured

logger = logging.getLogger("davi.admin")

router = APIRouter(prefix="/admin", tags=["admin"])

ALIAS_CARD_TYPE = "alias_card"


async def require_service_token(
    authorization: str | None = Header(default=None),
) -> None:
    settings = get_settings()
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not (
        settings.service_token
        and len(settings.service_token) >= 32
        and token
        and hmac.compare_digest(token, settings.service_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Service token required",
        )


def _alias_card_text(display_name: str, code: str, aliases: list[str]) -> str:
    listed = ", ".join(aliases)
    return (
        f"{display_name} ({code}) is also known as: {listed}. "
        f"Each of these is an alternate name for the same condition, "
        f"{display_name}."
    )


@router.post("/registry/{code}/refresh", dependencies=[Depends(require_service_token)])
async def refresh_registry_entry(
    code: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Re-sync the chat layer with a changed registry entry.

    Always resets the keyword-index cache. For active entries with aliases,
    replaces the alias-card chunk (embedded when the embedding service is
    configured — fail-open to keyword-only otherwise). Inactive entries get
    their alias card removed.
    """
    # Serialize concurrent refreshes of the same code (delete+insert below
    # would otherwise race into duplicate cards). Postgres-only; the sqlite
    # test variant is single-writer anyway.
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"alias_card:{code}"},
        )

    row = (
        await db.execute(
            select(ConditionRegistry).where(ConditionRegistry.condition_code == code)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown condition code: {code}")

    # Old card goes regardless — inactive entries keep nothing behind.
    await db.execute(
        delete(McpChunk).where(
            McpChunk.condition_code == code,
            McpChunk.chunk_type == ALIAS_CARD_TYPE,
        )
    )

    aliases = [a for a in (row.aliases or []) if a and a.strip()]
    card_written = False
    embedded = False
    if row.active and aliases:
        content = _alias_card_text(row.display_name, code, aliases)
        vectors = await embed_texts([content]) if embeddings_configured() else None
        db.add(
            McpChunk(
                condition_code=code,
                chunk_type=ALIAS_CARD_TYPE,
                content=content,
                embedding=vectors[0] if vectors else None,
                chunk_metadata={"source": "registry_refresh"},
            )
        )
        card_written = True
        embedded = vectors is not None

    await db.commit()
    # After commit so a rebuild can only ever see the new state.
    reset_index_cache()
    logger.info(
        "registry refresh: code=%s card=%s embedded=%s aliases=%d",
        code, card_written, embedded, len(aliases),
    )
    return {
        "ok": True,
        "condition_code": code,
        "active": bool(row.active),
        "aliases": aliases,
        "index_reset": True,
        "alias_card": card_written,
        "embedded": embedded,
    }

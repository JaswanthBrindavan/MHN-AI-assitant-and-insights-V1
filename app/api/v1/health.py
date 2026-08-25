"""Liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from app.telemetry import render_prometheus

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus exposition.

    Deliberately unauthenticated and deliberately free of PHI: label values
    come from bounded, code-defined sets, never from a message, a user id or a
    condition name. Keep it off the public internet the way /health is — it
    reveals traffic shape, not content.

    The series that matters most is ``davi_degradations_total``: this service
    has several fail-open paths that answer with a safe fallback and log a
    WARNING nobody reads. Without that counter it can be degrading at scale
    and look healthy.
    """
    return Response(
        content=render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
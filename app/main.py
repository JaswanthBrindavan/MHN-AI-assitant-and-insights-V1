"""FastAPI application factory and router wiring."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1 import (
    admin,
    chat,
    documents,
    feedback,
    health,
    insights,
    patterns,
    pedigree,
    profile,
    review,
)
from app.config import get_settings
from app.observability import request_id_middleware

API_V1 = "/api/v1"
_UI_INDEX = Path(__file__).resolve().parent.parent / "ui" / "index.html"


def create_app() -> FastAPI:
    # Audit R7: no logging configuration existed anywhere in app/, so
    # production had no root handler at all. get_settings() configures it
    # (see app/config.py) — calling it explicitly here rather than relying on
    # the incidental eager Settings() construction a couple of other modules
    # perform at import time.
    get_settings()

    app = FastAPI(
        title="Ink Health AI",
        version="0.1.0",
        summary="Decision-support backend — never diagnosis.",
    )

    # Audit R8/R10: zero middleware existed. A request id — inbound if the
    # caller (mhn-spring) already minted one, generated otherwise — on every
    # log line for the request and echoed back in the response. Deliberately
    # NOT adding CORS (Davi is called server-side by the React BFF and by
    # Spring, never directly from a browser — see docs/production_integration.md
    # "Frontend integration") or a rate limiter (the BFF already runs
    # chain(withRateLimit(), withAuth()) in front of every call here, and a
    # limiter at this layer would throttle by Spring's IP, not the end user's).
    app.add_middleware(BaseHTTPMiddleware, dispatch=request_id_middleware)

    # /health is unversioned for load balancers; also exposed under /api/v1.
    app.include_router(health.router)
    app.include_router(health.router, prefix=API_V1)
    app.include_router(pedigree.router, prefix=API_V1)
    app.include_router(insights.router, prefix=API_V1)
    # Behaviour patterns. Deliberately NOT under /insights: that route is
    # the family-history engine, and the app labelling both screens
    # "Insights" is presentation, not a reason to merge them.
    app.include_router(patterns.router, prefix=API_V1)
    app.include_router(chat.router, prefix=API_V1)
    app.include_router(documents.router, prefix=API_V1)
    app.include_router(admin.router, prefix=API_V1)
    app.include_router(profile.router, prefix=API_V1)
    app.include_router(feedback.router, prefix=API_V1)
    app.include_router(review.router, prefix=API_V1)

    # Self-contained test console (dev tool; synthetic accounts only).
    if _UI_INDEX.exists():
        @app.get("/", include_in_schema=False)
        async def test_console() -> FileResponse:
            return FileResponse(_UI_INDEX)

    return app


app = create_app()

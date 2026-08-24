"""FastAPI application factory and router wiring."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.v1 import admin, chat, documents, health, insights, pedigree

API_V1 = "/api/v1"
_UI_INDEX = Path(__file__).resolve().parent.parent / "ui" / "index.html"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Davi Health AI",
        version="0.1.0",
        summary="Decision-support backend — never diagnosis.",
    )

    # /health is unversioned for load balancers; also exposed under /api/v1.
    app.include_router(health.router)
    app.include_router(health.router, prefix=API_V1)
    app.include_router(pedigree.router, prefix=API_V1)
    app.include_router(insights.router, prefix=API_V1)
    app.include_router(chat.router, prefix=API_V1)
    app.include_router(documents.router, prefix=API_V1)
    app.include_router(admin.router, prefix=API_V1)

    # Self-contained test console (dev tool; synthetic accounts only).
    if _UI_INDEX.exists():
        @app.get("/", include_in_schema=False)
        async def test_console() -> FileResponse:
            return FileResponse(_UI_INDEX)

    return app


app = create_app()

"""FastAPI application factory and router wiring."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.v1 import chat, health, insights, pedigree

API_V1 = "/api/v1"


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

    return app


app = create_app()

"""FastAPI application factory and router wiring."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.v1 import health


def create_app() -> FastAPI:
    app = FastAPI(
        title="Davi Health AI",
        version="0.1.0",
        summary="Decision-support backend — never diagnosis.",
    )

    # /health is unversioned for load balancers; also exposed under /api/v1.
    app.include_router(health.router)
    app.include_router(health.router, prefix="/api/v1")

    return app


app = create_app()

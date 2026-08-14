"""Shared test fixtures.

Unit tests run against an in-memory aiosqlite database whose schema is built
directly from ``Base.metadata`` (no Alembic). A shared ``StaticPool`` keeps the
single in-memory connection alive across sessions within a test.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

# Import the models package so every table is registered before create_all.
import app.models  # noqa: F401
from app.db import Base, get_db
from app.main import create_app


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Enforce ON DELETE CASCADE on sqlite (off by default).
    @event.listens_for(eng.sync_engine, "connect")
    def _fk_pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def db_session(sessionmaker) -> AsyncGenerator[AsyncSession, None]:
    async with sessionmaker() as session:
        yield session


@pytest_asyncio.fixture
async def client(sessionmaker) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch):
    """Tests must never inherit a developer's .env (live providers, embedding
    services). Force the offline defaults and clear the settings cache."""
    from app.config import get_settings

    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("EMBEDDING_MODEL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_condition_index():
    """Isolate the process-level condition-registry cache between tests."""
    from app.knowledge.registry import reset_index_cache

    reset_index_cache()
    yield
    reset_index_cache()


@pytest.fixture
def set_grounding_mode(monkeypatch):
    """Set GROUNDING_MODE for a test and refresh the settings cache."""
    from app.config import get_settings

    def _set(mode: str) -> None:
        monkeypatch.setenv("GROUNDING_MODE", mode)
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()

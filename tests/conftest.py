"""Shared test fixtures.

Unit tests run against an in-memory aiosqlite database whose schema is built
directly from ``Base.metadata`` (no Alembic). A shared ``StaticPool`` keeps the
single in-memory connection alive across sessions within a test.
"""

from __future__ import annotations

import os as _os
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


# --------------------------------------------------------------------------- #
# PostgreSQL (opt-in)
# --------------------------------------------------------------------------- #
# `_hybrid_rank` returns None immediately on any non-PostgreSQL dialect, so on
# SQLite the ENTIRE hybrid retrieval path is skipped and has never run in CI.
# The same is true of pgvector similarity, PG enum binds and partial unique
# indexes. These fixtures let a test opt in to the real thing.
#
# Set TEST_DATABASE_URL to enable; without it the tests skip rather than fail,
# so a developer with no local PostgreSQL is not blocked.



def _pg_url() -> str | None:
    return _os.environ.get("TEST_DATABASE_URL")


@pytest_asyncio.fixture
async def pg_engine():
    url = _pg_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL not set — PostgreSQL tests skipped")
    eng = create_async_engine(url, future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def pg_session(pg_engine) -> AsyncGenerator[AsyncSession, None]:
    """A real PostgreSQL session, mirroring `db_session`.

    Use for anything whose behaviour differs by dialect. Everything else should
    keep using `db_session` — SQLite is far faster and the suite runs on every
    push.
    """
    maker = async_sessionmaker(pg_engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session

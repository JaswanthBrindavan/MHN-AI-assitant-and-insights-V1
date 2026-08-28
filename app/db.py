"""Async database engine, session factory, and FastAPI dependency."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        # pool_pre_ping revalidates pooled connections before use and
        # pool_recycle retires idle ones, so the first request after an idle
        # period never lands on a connection the server already dropped
        # (hosted Postgres closes idle connections; without this the first
        # post-idle chat request 500s and only the retry succeeds).
        settings = get_settings()
        kwargs: dict = {
            "future": True,
            "pool_pre_ping": True,
            "pool_recycle": 300,
            # A failed statement's traceback renders its bind parameters —
            # user UUIDs next to health values — into application logs. With
            # this, SQLAlchemy renders "[SQL parameters hidden]" instead;
            # set echo/logging locally when a parameter is genuinely needed.
            "hide_parameters": True,
        }
        # SQLite (tests) uses a pool that takes none of these.
        if not settings.database_url.startswith("sqlite"):
            kwargs.update(
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_timeout=settings.db_pool_timeout,
            )
        _engine = create_async_engine(settings.database_url, **kwargs)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmaker


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async session."""
    async with get_sessionmaker()() as session:
        yield session

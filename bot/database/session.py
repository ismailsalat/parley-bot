"""Async engine and session handling."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from bot.database.models import Base

log = logging.getLogger(__name__)


class Database:
    def __init__(self, url: str, connect_args: dict | None = None) -> None:
        self.url = url
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"timeout": 30}
        else:
            kwargs.update(pool_size=5, max_overflow=5, pool_recycle=1800, connect_args=connect_args or {})
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        self._sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A unit of work: commits on success, rolls back on any error."""
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def ping(self) -> None:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def create_all(self) -> None:
        """Create tables directly. Only used by tests; production uses Alembic."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
        log.info("Database connections closed")

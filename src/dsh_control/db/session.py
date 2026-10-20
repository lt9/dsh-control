"""engine / session 工厂。测试用 aiosqlite 内存库（StaticPool 共享同一连接）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from .models import Base


def make_engine(url: str) -> AsyncEngine:
    kwargs: dict = {"echo": False}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url in ("sqlite+aiosqlite://", "sqlite+aiosqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    return create_async_engine(url, **kwargs)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@dataclass(slots=True)
class Db:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    def session(self) -> AsyncSession:
        return self.session_factory()


def make_db(url: str) -> Db:
    engine = make_engine(url)
    return Db(engine=engine, session_factory=make_session_factory(engine))


async def create_all(db: Db) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db_session(db: Db) -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖用。"""
    async with db.session() as session:
        yield session

"""SQLAlchemy base + async engine."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all models."""


_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict = {"future": True}
        # SQLite (test mode) uses StaticPool and doesn't accept pool_size /
        # pool_pre_ping. Production Postgres needs both.
        if settings.database_null_pool:
            from sqlalchemy.pool import NullPool
            kwargs["poolclass"] = NullPool
        elif not settings.database_url.startswith("sqlite"):
            kwargs["pool_size"] = settings.database_pool_size
            kwargs["pool_pre_ping"] = True
        _engine = create_async_engine(settings.database_url, **kwargs)
    return _engine

"""SQLAlchemy base — same pattern as dbt-runner."""
from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict = {"future": True}
        if settings.database_pool_disabled:
            from sqlalchemy.pool import NullPool
            kwargs["poolclass"] = NullPool
        elif not settings.database_url.startswith("sqlite"):
            kwargs["pool_size"] = settings.database_pool_size
            kwargs["pool_pre_ping"] = True
        _engine = create_async_engine(settings.effective_database_url(), **kwargs)
    return _engine

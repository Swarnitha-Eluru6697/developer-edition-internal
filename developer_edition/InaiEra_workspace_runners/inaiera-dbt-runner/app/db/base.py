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
        url = settings.effective_database_url
        kwargs: dict = {"future": True}
        # SQLite (test mode) uses StaticPool and doesn't accept pool_size /
        # pool_pre_ping. Production Postgres needs both.
        if not url.startswith("sqlite"):
            kwargs["pool_size"] = settings.database_pool_size
            kwargs["pool_pre_ping"] = True
            connect_args = settings.database_connect_args()
            if connect_args:
                kwargs["connect_args"] = connect_args
        _engine = create_async_engine(url, **kwargs)
    return _engine

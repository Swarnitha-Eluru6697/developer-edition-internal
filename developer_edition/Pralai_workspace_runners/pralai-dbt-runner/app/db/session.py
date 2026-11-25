"""Async session factory."""
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.db.base import get_engine

engine = get_engine()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

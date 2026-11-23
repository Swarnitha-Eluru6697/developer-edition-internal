from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.core.config import get_settings

def _make_engine():
    url = get_settings().database_url
    kwargs = {} if "sqlite" in url else {"pool_size": 10}
    return create_async_engine(url, echo=False, **kwargs)

engine = _make_engine()
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

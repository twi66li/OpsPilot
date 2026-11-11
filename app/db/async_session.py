"""HTTP业务使用异步驱动；同步调查图和入库线程保留各自独立Session。"""
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.base import shared_database_url

_engine = None


def async_engine():
    global _engine
    if _engine is None:
        url = make_url(shared_database_url())
        drivers = {"postgresql": "postgresql+asyncpg", "sqlite": "sqlite+aiosqlite", "mysql": "mysql+aiomysql"}
        backend = url.get_backend_name()
        options = {"pool_size": 3, "max_overflow": 2} if backend != "sqlite" else {"poolclass": NullPool}
        _engine = create_async_engine(url.set(drivername=drivers[backend]), pool_pre_ping=True, **options)
    return _engine


async def get_async_db():
    factory = async_sessionmaker(async_engine(), expire_on_commit=False)
    async with factory() as db:
        yield db


async def close_async_db():
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None

"""SQLAlchemy 引擎与会话管理。生产用 PostgreSQL，测试/开发可退化为 SQLite。"""
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import QueuePool

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(database_url: str):
    if database_url.startswith("sqlite"):
        if ":memory:" in database_url or "mode=memory" in database_url:
            # 共享内存库允许独立连接，避免一个会话退出时回滚另一个会话的事务。
            limits = {} if "cache=shared" in database_url else {"pool_size": 1, "max_overflow": 0}
            return create_engine(
                database_url,
                connect_args={"check_same_thread": False},
                poolclass=QueuePool,
                **limits,
            )
        db_path = database_url.replace("sqlite:///", "", 1)
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            os.makedirs(parent, exist_ok=True)
        return create_engine(database_url, connect_args={"check_same_thread": False})
    return create_engine(database_url, pool_pre_ping=True, pool_recycle=3600)


def shared_database_url():
    url = get_settings().database_url
    # 测试的同步图与异步接口使用同一个进程内SQLite库，仍不落盘。
    if url == "sqlite:///:memory:":
        return f"sqlite:///file:opspilot-{os.getpid()}?mode=memory&cache=shared&uri=true"
    return url


engine = _make_engine(shared_database_url())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

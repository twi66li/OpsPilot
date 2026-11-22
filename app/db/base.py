"""SQLAlchemy 同步引擎与工作单元会话管理。生产用 PostgreSQL，测试/开发可退化为 SQLite。"""

import os
import random
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import QueuePool

from app.config import DbConnectionBudget, calculate_db_connection_budget, get_settings

__all__ = [
    "Base",
    "DbConnectionBudget",
    "SessionLocal",
    "_make_engine",
    "calculate_db_connection_budget",
    "engine",
    "get_db",
    "init_db",
    "is_sqlite_lock_error",
    "run_with_sqlite_retry",
    "shared_database_url",
]


class Base(DeclarativeBase):
    pass


def _attach_sqlite_pragmas(eng) -> None:
    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()


def _make_engine(database_url: str, settings=None):
    if database_url.startswith("sqlite"):
        if ":memory:" in database_url or "mode=memory" in database_url:
            # 共享内存库允许独立连接，避免一个会话退出时回滚另一个会话的事务。
            limits = {} if "cache=shared" in database_url else {"pool_size": 1, "max_overflow": 0}
            eng = create_engine(
                database_url,
                connect_args={"check_same_thread": False, "timeout": 30},
                poolclass=QueuePool,
                **limits,
            )
            _attach_sqlite_pragmas(eng)
            return eng
        db_path = database_url.replace("sqlite:///", "", 1)
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            os.makedirs(parent, exist_ok=True)
        eng = create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 30})
        _attach_sqlite_pragmas(eng)
        return eng
    cfg = settings or get_settings()
    budget = calculate_db_connection_budget(cfg)
    return create_engine(
        database_url,
        pool_size=budget.app_pool_size,
        max_overflow=budget.app_max_overflow,
        pool_timeout=cfg.db_pool_timeout,
        pool_pre_ping=True,
        pool_recycle=3600,
    )


def shared_database_url():
    url = get_settings().database_url
    # 测试的图执行线程与 HTTP 请求线程共享同一个进程内 SQLite 内存库，不落盘。
    if url == "sqlite:///:memory:":
        return f"sqlite:///file:opspilot-{os.getpid()}?mode=memory&cache=shared&uri=true"
    return url


def is_sqlite_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def run_with_sqlite_retry(fn, *, max_retries: int = 40):
    """SQLite 并发事务短暂锁冲突时做短退避重试；PostgreSQL 下单次执行直接返回。"""
    for attempt in range(max_retries):
        try:
            return fn()
        except OperationalError as exc:
            if not is_sqlite_lock_error(exc) or attempt + 1 >= max_retries:
                raise
            time.sleep(min(0.05, 0.002 * (attempt + 1)) + random.uniform(0, 0.005))


engine = _make_engine(shared_database_url())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def postgres_lock(key: str, *, blocking=True):
    """持有 PostgreSQL 会话锁，在调用方多个事务之间协调副本。"""
    if engine.dialect.name != "postgresql":
        yield True
        return
    with engine.connect() as connection:
        params = {"key": key}
        while True:
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), params)
            connection.commit()
            if acquired or not blocking:
                break
            # 等待者要结束事务，以免阻塞持锁副本创建检查点并发索引。
            time.sleep(0.1)
        try:
            yield acquired
        finally:
            if acquired:
                connection.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), params)
                connection.commit()


def init_db() -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('opspilot:migrations', 0))"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

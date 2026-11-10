"""SQLAlchemy 引擎与会话管理。生产用 MySQL，测试/开发可退化为 SQLite。"""
import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(database_url: str):
    if database_url.startswith("sqlite"):
        # 支持 sqlite:///:memory:（配合 StaticPool 让多线程共享同一内存库）
        if ":memory:" in database_url:
            from sqlalchemy.pool import StaticPool

            return create_engine(
                database_url,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        db_path = database_url.replace("sqlite:///", "", 1)
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            os.makedirs(parent, exist_ok=True)
        return create_engine(database_url, connect_args={"check_same_thread": False})
    return create_engine(database_url, pool_pre_ping=True, pool_recycle=3600)


engine = _make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app.db import models  # noqa: F401  确保模型注册

    Base.metadata.create_all(bind=engine)
    if "active_version_id" not in {column["name"] for column in inspect(engine).get_columns("documents")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE documents ADD COLUMN active_version_id VARCHAR(64) NULL"))
    # 旧工单保留为 NULL；只有新审批才能可靠关联编号，不猜测历史关联。
    if "approval_id" not in {column["name"] for column in inspect(engine).get_columns("tickets")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE tickets ADD COLUMN approval_id VARCHAR(64) NULL"))
    for index in models.Ticket.__table__.indexes:
        if index.name == "uq_tickets_approval_id":
            index.create(bind=engine, checkfirst=True)

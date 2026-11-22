"""数据库预算账本 ORM 模型：作用域主表、预占/结算明细表与审计事件表。"""

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> datetime:
    # 与现有模型保持一致：DateTime 列统一存 UTC naive 时间。
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BudgetLedgerState(Base):
    """按作用域汇总的权威预算状态；所有并发写事务均以本表作用域行作为排他锁根。"""

    __tablename__ = "budget_ledgers"

    scope: Mapped[str] = mapped_column(String(128), primary_key=True)
    token_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model_limits: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    actual_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    unknown_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    in_flight_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    model_usage: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    halted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    halted_reason: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class BudgetReservation(Base):
    """单笔调用预占与结算生命周期记录（reserved -> committed | committed_unknown | released）。"""

    __tablename__ = "budget_reservations"
    __table_args__ = (
        Index("uq_budget_res_scope_idem", "scope", "idempotency_key", unique=True),
        Index("uq_budget_res_scope_settle", "scope", "settlement_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(128), ForeignKey("budget_ledgers.scope"), index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    settlement_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    estimate: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default="reserved", server_default="reserved", index=True, nullable=False
    )
    over_reserve: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BudgetLedgerEvent(Base):
    """预算账本关键状态变更审计（halt / clearance / migration），带作用域幂等键。"""

    __tablename__ = "budget_ledger_events"
    __table_args__ = (Index("uq_budget_event_scope_idem", "scope", "idempotency_key", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(128), ForeignKey("budget_ledgers.scope"), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    reservation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pid: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

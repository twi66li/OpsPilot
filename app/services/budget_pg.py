"""基于 PostgreSQL / SQLite 数据库事务的权威预算账本。

设计要点：
- 与 JSONL BudgetLedger 完全对齐：总上限 + 分模型授权上限、可证明保守上界、
  actual / unknown / in_flight 三态计数、实报超预留即 halt 与显式 clearance 解除；
- 并发安全：PostgreSQL 使用 SELECT ... FOR UPDATE 锁 budget_ledgers 作用域行，
  SQLite 在事务首语句直接以 UPDATE 抢占写锁并配合短退避重试，多副本/多进程无 OS 锁冲突；
- 三步生命周期与唯一幂等键：
  - reserve(..., idempotency_key=...)：预占额度，同作用域重复幂等键返回原 rid 不重复扣额；
  - commit(rid, usage, idempotency_key=...)：按实报结算，重复同参结算幂等，实报超预留全额入账并 halt；
  - commit_unknown(rid, idempotency_key=...)：请求已发供应商但超时/缺 usage，转 unknown 保留风险额度；
  - release(rid, idempotency_key=...)：确认未发供应商时释放额度，重复释放幂等，已结算不可释放。
"""

import logging
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.db.base import _make_engine, is_sqlite_lock_error, run_with_sqlite_retry
from app.db.budget_models import BudgetLedgerEvent, BudgetLedgerState, BudgetReservation

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dialect_name(db) -> str:
    bind = db.get_bind()
    return getattr(getattr(bind, "dialect", None), "name", "")


def _normalize_model_limits(model_limits: dict[str, int]) -> dict[str, int]:
    from app.services.budget import BudgetLedgerError

    if not model_limits:
        raise BudgetLedgerError("必须配置分模型授权额度（budget.json p1 列），未授权模型一律拒绝")
    normalized: dict[str, int] = {}
    for model, cap in model_limits.items():
        if isinstance(cap, bool) or int(cap) <= 0:
            raise BudgetLedgerError("必须配置分模型授权额度（budget.json p1 列），未授权模型一律拒绝")
        normalized[str(model)] = int(cap)
    return normalized


def _clone_model_usage(
    raw: dict | None, model_limits: dict[str, int] | None = None
) -> dict[str, dict[str, int]]:
    usage: dict[str, dict[str, int]] = {}
    if isinstance(raw, dict):
        for model, bucket in raw.items():
            usage[str(model)] = {
                "actual": int(bucket.get("actual", 0)),
                "unknown": int(bucket.get("unknown", 0)),
                "in_flight": int(bucket.get("in_flight", 0)),
            }
    if model_limits:
        for model in model_limits:
            usage.setdefault(str(model), {"actual": 0, "unknown": 0, "in_flight": 0})
    return usage


def _extract_positive_actual(usage_or_actual) -> int | None:
    from app.services.budget import _total_tokens

    if isinstance(usage_or_actual, bool):
        return None
    if isinstance(usage_or_actual, (int, float)):
        value = int(usage_or_actual)
        return value if value > 0 else None
    return _total_tokens(usage_or_actual)


def _validate_idem_key(key: str | None, label: str) -> str | None:
    from app.services.budget import BudgetLedgerError

    if key is None:
        return None
    if not isinstance(key, str) or not key.strip():
        raise BudgetLedgerError(f"{label} 不能为空字符串")
    if len(key) > 128:
        raise BudgetLedgerError(f"{label} 长度不能超过 128")
    return key


class DatabaseBudgetLedger:
    """基于数据库行级事务锁与幂等键的共享预算账本。"""

    def __init__(
        self,
        *,
        scope: str = "default",
        limit: int | None = None,
        model_limits: dict[str, int] | None = None,
        session_factory=None,
        engine=None,
        database_url: str | None = None,
        settings=None,
    ):
        from app.services.budget import BudgetLedgerError

        if not scope or not isinstance(scope, str) or not scope.strip():
            raise BudgetLedgerError("预算账本 scope 不能为空")
        self.scope = scope.strip()
        self.corrupt = False
        self.corrupt_detail = ""
        self._owns_engine = False
        self._engine = None

        if session_factory is not None:
            self._session_factory = session_factory
        elif engine is not None:
            self._engine = engine
            self._session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        elif database_url is not None:
            self._engine = _make_engine(database_url, settings=settings)
            self._owns_engine = True
            self._session_factory = sessionmaker(bind=self._engine, autoflush=False, expire_on_commit=False)
        else:
            from app.db.base import SessionLocal

            self._session_factory = SessionLocal

        if limit is None and model_limits is None:
            state = self._load_existing_scope()
            self.limit = int(state["limit"])
            self.model_limits = dict(state["model_limits"])
            return

        if limit is None or model_limits is None:
            raise BudgetLedgerError("初始化数据库预算账本必须同时提供总上限和分模型授权额度")
        if isinstance(limit, bool) or int(limit) <= 0:
            raise BudgetLedgerError("预算总上限必须为正，拒绝在无上限时放行真实调用")
        normalized_limits = _normalize_model_limits(model_limits)
        self.limit = int(limit)
        self.model_limits = normalized_limits
        self._ensure_scope(self.limit, self.model_limits)

    # ---- 内部事务与锁原语 ----
    def _run_tx(self, fn):
        from app.services.budget import BudgetExceededError, BudgetLedgerError

        def _once():
            db = self._session_factory()
            try:
                result = fn(db)
                db.commit()
                return result
            except (BudgetExceededError, BudgetLedgerError):
                db.rollback()
                raise
            except OperationalError:
                db.rollback()
                raise
            except SQLAlchemyError as exc:
                db.rollback()
                raise BudgetLedgerError(f"数据库预算账本写入失败：{type(exc).__name__}") from exc
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        try:
            return run_with_sqlite_retry(_once, max_retries=80)
        except OperationalError as exc:
            if is_sqlite_lock_error(exc):
                raise BudgetLedgerError("数据库预算账本锁等待超时，fail closed") from exc
            raise BudgetLedgerError(f"数据库预算账本操作失败：{type(exc).__name__}") from exc

    def _lock_scope(self, db) -> BudgetLedgerState | None:
        """排他锁定作用域行：PG 使用 FOR UPDATE，SQLite 用首条 UPDATE 抢占 RESERVED 写锁。"""
        dialect = _dialect_name(db)
        if dialect == "postgresql":
            return (
                db.query(BudgetLedgerState)
                .filter_by(scope=self.scope)
                .populate_existing()
                .with_for_update()
                .first()
            )
        if dialect == "sqlite":
            updated = db.execute(
                update(BudgetLedgerState)
                .where(BudgetLedgerState.scope == self.scope)
                .values(updated_at=_now())
            ).rowcount
            if updated == 0:
                return None
            return db.query(BudgetLedgerState).filter_by(scope=self.scope).populate_existing().first()
        return db.query(BudgetLedgerState).filter_by(scope=self.scope).populate_existing().first()

    def _load_existing_scope(self) -> dict:
        from app.services.budget import BudgetLedgerError

        def _read(db):
            row = db.get(BudgetLedgerState, self.scope)
            if row is None:
                raise BudgetLedgerError(f"数据库预算账本作用域 {self.scope!r} 不存在，拒绝在无上限时放行")
            return self._snapshot_from_row(row)

        return self._run_tx(_read)

    def _ensure_scope(self, limit: int, model_limits: dict[str, int]) -> None:
        def _init_or_sync(db):
            row = self._lock_scope(db)
            now = _now()
            if row is None:
                try:
                    row = BudgetLedgerState(
                        scope=self.scope,
                        token_limit=limit,
                        model_limits=dict(model_limits),
                        actual_tokens=0,
                        unknown_tokens=0,
                        in_flight_tokens=0,
                        model_usage=_clone_model_usage({}, model_limits),
                        halted=False,
                        halted_reason="",
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(row)
                    db.flush()
                    return
                except IntegrityError:
                    db.rollback()
                    row = self._lock_scope(db)
                    if row is None:
                        raise
            row.token_limit = limit
            row.model_limits = dict(model_limits)
            row.model_usage = _clone_model_usage(row.model_usage, model_limits)
            row.updated_at = now

        self._run_tx(_init_or_sync)

    @staticmethod
    def _snapshot_from_row(row: BudgetLedgerState) -> dict:
        model_limits = {str(m): int(v) for m, v in (row.model_limits or {}).items()}
        models = _clone_model_usage(row.model_usage, model_limits)
        actual = int(row.actual_tokens)
        unknown = int(row.unknown_tokens)
        in_flight = int(row.in_flight_tokens)
        limit = int(row.token_limit)
        used = actual + unknown + in_flight
        model_remaining = {
            m: cap - (models[m]["actual"] + models[m]["unknown"] + models[m]["in_flight"])
            for m, cap in model_limits.items()
        }
        return {
            "scope": row.scope,
            "actual": actual,
            "unknown": unknown,
            "in_flight": in_flight,
            "limit": limit,
            "remaining": limit - used,
            "models": models,
            "model_limits": model_limits,
            "model_remaining": model_remaining,
            "halted": bool(row.halted),
            "halted_reason": str(row.halted_reason or ""),
            "corrupt": False,
        }

    # ---- 动态状态属性（多进程实时可见） ----
    @property
    def halted(self) -> bool:
        return bool(self.usage_now()["halted"])

    @property
    def halted_reason(self) -> str:
        return str(self.usage_now()["halted_reason"])

    # ---- 三步生命周期接口：reserve / commit / commit_unknown / release ----
    def reserve(self, kind: str, model: str, estimate: int, *, idempotency_key: str | None = None) -> str:
        """发送前事务预占；同作用域重复幂等键直接返回原 rid，不重复扣减额度。"""
        from app.services.budget import BudgetExceededError, BudgetLedgerError

        idem = _validate_idem_key(idempotency_key, "预占幂等键")
        if isinstance(estimate, bool) or int(estimate) < 0:
            raise BudgetExceededError("预占估算必须为非负整数，发送前拒绝")
        est = int(estimate)

        def _tx(db):
            ledger = self._lock_scope(db)
            if ledger is None:
                raise BudgetLedgerError(f"预算账本作用域 {self.scope!r} 不存在，fail closed")

            if idem is not None:
                existing = (
                    db.query(BudgetReservation)
                    .filter_by(scope=self.scope, idempotency_key=idem)
                    .first()
                )
                if existing is not None:
                    if existing.kind != kind or existing.model != model or int(existing.estimate) != est:
                        raise BudgetLedgerError(
                            f"预占幂等键 {idem!r} 已绑定到不同请求参数（model={existing.model}, estimate={existing.estimate}）"
                        )
                    return existing.id

            if ledger.halted:
                raise BudgetExceededError(
                    f"预算已停止：{ledger.halted_reason}；需人工核查后写入 clearance 才能恢复"
                )
            model_limits = {str(m): int(v) for m, v in (ledger.model_limits or {}).items()}
            if model not in model_limits:
                raise BudgetExceededError(f"模型 {model!r} 不在授权清单内，请求在到达供应商前被拒绝")

            actual = int(ledger.actual_tokens)
            unknown = int(ledger.unknown_tokens)
            in_flight = int(ledger.in_flight_tokens)
            token_limit = int(ledger.token_limit)
            used = actual + unknown + in_flight
            if used + est > token_limit:
                raise BudgetExceededError(
                    f"预算超限：实报 {actual} + 未知 {unknown} + 在途 {in_flight} + 本次预估 {est}"
                    f" = {used + est} 超过总授权 {token_limit}，请求在到达供应商前被拒绝"
                )

            usage = _clone_model_usage(ledger.model_usage, model_limits)
            bucket = usage[model]
            model_used_total = bucket["actual"] + bucket["unknown"] + bucket["in_flight"]
            model_cap = model_limits[model]
            if model_used_total + est > model_cap:
                raise BudgetExceededError(
                    f"模型 {model} 额度超限：实报 {bucket['actual']} + 未知 {bucket['unknown']}"
                    f" + 在途 {bucket['in_flight']} + 本次预估 {est} = {model_used_total + est}"
                    f" 超过授权 {model_cap}，请求在到达供应商前被拒绝"
                )

            rid = uuid.uuid4().hex
            now = _now()
            db.add(
                BudgetReservation(
                    id=rid,
                    scope=self.scope,
                    idempotency_key=idem if idem is not None else rid,
                    kind=kind,
                    model=model,
                    estimate=est,
                    status="reserved",
                    over_reserve=False,
                    pid=os.getpid(),
                    note="",
                    reserved_at=now,
                )
            )
            bucket["in_flight"] += est
            ledger.in_flight_tokens = in_flight + est
            ledger.model_usage = usage
            ledger.updated_at = now
            return rid

        return self._run_tx(_tx)

    def _check_settlement_key_owner(self, db, rid: str, settlement_key: str | None) -> None:
        from app.services.budget import BudgetLedgerError

        if settlement_key is None:
            return
        conflict = (
            db.query(BudgetReservation)
            .filter_by(scope=self.scope, settlement_key=settlement_key)
            .first()
        )
        if conflict is not None and conflict.id != rid:
            raise BudgetLedgerError(f"结算幂等键 {settlement_key!r} 已被预留 {conflict.id} 占用")

    def commit(
        self,
        rid: str,
        usage_or_actual,
        *,
        idempotency_key: str | None = None,
        note: str = "",
    ) -> dict:
        """按实际消耗结算；重复相同参数/幂等键结算幂等；实报超预留全额入账并触发 halt。"""
        from app.services.budget import BudgetLedgerError

        settle_key = _validate_idem_key(idempotency_key, "结算幂等键")
        actual = _extract_positive_actual(usage_or_actual)
        if actual is None:
            raise BudgetLedgerError("commit 缺少有效正整数实报 token，缺 usage 请调用 commit_unknown")

        def _tx(db):
            ledger = self._lock_scope(db)
            if ledger is None:
                raise BudgetLedgerError(f"预算账本作用域 {self.scope!r} 不存在，fail closed")
            res = db.query(BudgetReservation).filter_by(id=rid, scope=self.scope).first()
            if res is None:
                raise BudgetLedgerError(f"结算找不到预留 {rid}，不静默覆盖")
            self._check_settlement_key_owner(db, rid, settle_key)

            if res.status == "committed":
                if int(res.actual or 0) == actual and (settle_key is None or res.settlement_key == settle_key):
                    return {
                        "id": rid,
                        "status": "committed",
                        "actual": int(res.actual or 0),
                        "over_reserve": bool(res.over_reserve),
                        "idempotent": True,
                    }
                raise BudgetLedgerError(
                    f"预留 {rid} 已按 actual={res.actual} 结算（key={res.settlement_key}），拒绝重复覆盖"
                )
            if res.status != "reserved":
                raise BudgetLedgerError(f"预留 {rid} 已处于终态 {res.status}，不能再次 commit")

            now = _now()
            est = int(res.estimate)
            over = actual > est
            usage = _clone_model_usage(ledger.model_usage, ledger.model_limits)
            bucket = usage.setdefault(res.model, {"actual": 0, "unknown": 0, "in_flight": 0})

            bucket["in_flight"] -= est
            bucket["actual"] += actual
            ledger.in_flight_tokens = int(ledger.in_flight_tokens) - est
            ledger.actual_tokens = int(ledger.actual_tokens) + actual
            ledger.model_usage = usage
            ledger.updated_at = now

            res.status = "committed"
            res.actual = actual
            res.over_reserve = over
            res.settlement_key = settle_key or f"commit:{rid}"
            res.settled_at = now
            if note:
                res.note = note

            if over:
                reason = f"实报 {actual} 超预留 {est}（记录 {rid}），需人工核查"
                ledger.halted = True
                ledger.halted_reason = reason
                db.add(
                    BudgetLedgerEvent(
                        scope=self.scope,
                        event_type="halt",
                        idempotency_key=f"halt:{rid}",
                        reservation_id=rid,
                        pid=os.getpid(),
                        note=reason,
                        detail={"model": res.model, "estimate": est, "actual": actual},
                        created_at=now,
                    )
                )

            return {
                "id": rid,
                "status": "committed",
                "actual": actual,
                "over_reserve": over,
                "idempotent": False,
            }

        return self._run_tx(_tx)

    def commit_unknown(
        self,
        rid: str,
        *,
        idempotency_key: str | None = None,
        reason: str = "",
    ) -> dict:
        """请求已发供应商但超时/缺 usage：将预占转为 unknown 保留风险额度，绝不按零释放。"""
        from app.services.budget import BudgetLedgerError

        settle_key = _validate_idem_key(idempotency_key, "结算幂等键")

        def _tx(db):
            ledger = self._lock_scope(db)
            if ledger is None:
                raise BudgetLedgerError(f"预算账本作用域 {self.scope!r} 不存在，fail closed")
            res = db.query(BudgetReservation).filter_by(id=rid, scope=self.scope).first()
            if res is None:
                raise BudgetLedgerError(f"结算找不到预留 {rid}，不静默覆盖")
            self._check_settlement_key_owner(db, rid, settle_key)

            if res.status == "committed_unknown":
                if settle_key is None or res.settlement_key == settle_key:
                    return {
                        "id": rid,
                        "status": "committed_unknown",
                        "unknown": int(res.estimate),
                        "idempotent": True,
                    }
                raise BudgetLedgerError(
                    f"预留 {rid} 已记 committed_unknown（key={res.settlement_key}），幂等键冲突"
                )
            if res.status != "reserved":
                raise BudgetLedgerError(f"预留 {rid} 已处于终态 {res.status}，不能转为 committed_unknown")

            now = _now()
            est = int(res.estimate)
            usage = _clone_model_usage(ledger.model_usage, ledger.model_limits)
            bucket = usage.setdefault(res.model, {"actual": 0, "unknown": 0, "in_flight": 0})
            bucket["in_flight"] -= est
            bucket["unknown"] += est

            ledger.in_flight_tokens = int(ledger.in_flight_tokens) - est
            ledger.unknown_tokens = int(ledger.unknown_tokens) + est
            ledger.model_usage = usage
            ledger.updated_at = now

            res.status = "committed_unknown"
            res.settlement_key = settle_key or f"unknown:{rid}"
            res.settled_at = now
            if reason:
                res.note = reason

            return {
                "id": rid,
                "status": "committed_unknown",
                "unknown": est,
                "idempotent": False,
            }

        return self._run_tx(_tx)

    def release(
        self,
        rid: str,
        *,
        idempotency_key: str | None = None,
        reason: str = "",
    ) -> dict:
        """确认供应商未收到请求时释放预占额度；重复释放幂等，已结算记录禁止释放。"""
        from app.services.budget import BudgetLedgerError

        settle_key = _validate_idem_key(idempotency_key, "释放幂等键")

        def _tx(db):
            ledger = self._lock_scope(db)
            if ledger is None:
                raise BudgetLedgerError(f"预算账本作用域 {self.scope!r} 不存在，fail closed")
            res = db.query(BudgetReservation).filter_by(id=rid, scope=self.scope).first()
            if res is None:
                raise BudgetLedgerError(f"释放找不到预留 {rid}，拒绝操作")
            self._check_settlement_key_owner(db, rid, settle_key)

            if res.status == "released":
                if settle_key is None or res.settlement_key == settle_key:
                    return {
                        "id": rid,
                        "status": "released",
                        "released": int(res.estimate),
                        "idempotent": True,
                    }
                raise BudgetLedgerError(
                    f"预留 {rid} 已释放（key={res.settlement_key}），释放幂等键冲突"
                )
            if res.status in ("committed", "committed_unknown"):
                raise BudgetLedgerError(f"预留 {rid} 已结算（{res.status}），不可释放")
            if res.status != "reserved":
                raise BudgetLedgerError(f"预留 {rid} 状态异常（{res.status}），不可释放")

            now = _now()
            est = int(res.estimate)
            usage = _clone_model_usage(ledger.model_usage, ledger.model_limits)
            bucket = usage.setdefault(res.model, {"actual": 0, "unknown": 0, "in_flight": 0})
            bucket["in_flight"] -= est

            ledger.in_flight_tokens = int(ledger.in_flight_tokens) - est
            ledger.model_usage = usage
            ledger.updated_at = now

            res.status = "released"
            res.settlement_key = settle_key or f"release:{rid}"
            res.settled_at = now
            if reason:
                res.note = reason

            return {
                "id": rid,
                "status": "released",
                "released": est,
                "idempotent": False,
            }

        return self._run_tx(_tx)

    def settle(self, rid: str, usage, *, idempotency_key: str | None = None) -> None:
        """兼容 BudgetLedger 的统一结算接口：有实报走 commit，缺失/超时走 commit_unknown。"""
        actual = _extract_positive_actual(usage)
        if actual is None:
            self.commit_unknown(rid, idempotency_key=idempotency_key)
        else:
            self.commit(rid, actual, idempotency_key=idempotency_key)

    def clear_halt(self, note: str, *, idempotency_key: str | None = None) -> None:
        """人工核查后显式解除 halt，并落库 clearance 审计事件。"""
        from app.services.budget import BudgetLedgerError

        event_key = _validate_idem_key(idempotency_key, "clearance 幂等键")

        def _tx(db):
            ledger = self._lock_scope(db)
            if ledger is None:
                raise BudgetLedgerError(f"预算账本作用域 {self.scope!r} 不存在，fail closed")
            if event_key is not None:
                existed = (
                    db.query(BudgetLedgerEvent)
                    .filter_by(scope=self.scope, idempotency_key=event_key)
                    .first()
                )
                if existed is not None:
                    return
            if not ledger.halted:
                return
            now = _now()
            ledger.halted = False
            ledger.halted_reason = ""
            ledger.updated_at = now
            db.add(
                BudgetLedgerEvent(
                    scope=self.scope,
                    event_type="clearance",
                    idempotency_key=event_key or f"clearance:{uuid.uuid4().hex}",
                    reservation_id=None,
                    pid=os.getpid(),
                    note=note,
                    detail={},
                    created_at=now,
                )
            )

        self._run_tx(_tx)

    def usage_now(self) -> dict:
        """读取作用域权威预算快照（实时反映跨进程状态）。"""
        from app.services.budget import BudgetLedgerError

        def _read(db):
            row = db.get(BudgetLedgerState, self.scope)
            if row is None:
                raise BudgetLedgerError(f"预算账本作用域 {self.scope!r} 不存在，fail closed")
            return self._snapshot_from_row(row)

        return self._run_tx(_read)

    def list_unsettled_ids(self) -> list[str]:
        """列出当前仍在途（status=reserved）的预占 ID。"""

        def _read(db):
            rows = (
                db.query(BudgetReservation.id)
                .filter_by(scope=self.scope, status="reserved")
                .order_by(BudgetReservation.reserved_at.asc(), BudgetReservation.id.asc())
                .all()
            )
            return [r[0] for r in rows]

        return self._run_tx(_read)

    def verify_consistency(self) -> dict:
        """从明细表重算三态总额与分模型分项，核对与 budget_ledgers 主表 100% 一致。"""
        from app.services.budget import BudgetLedgerError

        def _check(db):
            row = db.get(BudgetLedgerState, self.scope)
            if row is None:
                raise BudgetLedgerError(f"预算账本作用域 {self.scope!r} 不存在")
            snap = self._snapshot_from_row(row)
            recomputed_actual = 0
            recomputed_unknown = 0
            recomputed_in_flight = 0
            recomputed_models = {
                m: {"actual": 0, "unknown": 0, "in_flight": 0} for m in snap["model_limits"]
            }
            reservations = db.query(BudgetReservation).filter_by(scope=self.scope).all()
            for res in reservations:
                bucket = recomputed_models.setdefault(
                    res.model, {"actual": 0, "unknown": 0, "in_flight": 0}
                )
                est = int(res.estimate)
                if res.status == "reserved":
                    recomputed_in_flight += est
                    bucket["in_flight"] += est
                elif res.status == "committed":
                    act = int(res.actual or 0)
                    recomputed_actual += act
                    bucket["actual"] += act
                elif res.status == "committed_unknown":
                    recomputed_unknown += est
                    bucket["unknown"] += est
                elif res.status == "released":
                    pass
                else:
                    raise BudgetLedgerError(f"发现未知预占状态：{res.status!r}")

            all_models = set(snap["models"]) | set(recomputed_models)
            zero_bucket = {"actual": 0, "unknown": 0, "in_flight": 0}
            models_match = all(
                snap["models"].get(m, zero_bucket) == recomputed_models.get(m, zero_bucket)
                for m in all_models
            )
            if (
                recomputed_actual != snap["actual"]
                or recomputed_unknown != snap["unknown"]
                or recomputed_in_flight != snap["in_flight"]
                or not models_match
            ):
                raise BudgetLedgerError(
                    f"预算汇总与明细不一致：summary=({snap['actual']}, {snap['unknown']}, {snap['in_flight']}), "
                    f"detail=({recomputed_actual}, {recomputed_unknown}, {recomputed_in_flight})"
                )
            return snap

        return self._run_tx(_check)

    def close(self) -> None:
        """若账本持有独立数据库引擎则释放连接池；复用全局引擎时不关闭全局池。"""
        if self._owns_engine and self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._owns_engine = False

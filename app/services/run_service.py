"""按 run_id 记录运行事件；上下文跟随执行线程，迟到的只读请求用量仍可追加。"""

import asyncio
import copy
import hashlib
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_current = ContextVar("agent_run", default=None)
_deadline = ContextVar("tool_deadline", default=None)
_agent = ContextVar("agent_name", default="main")
_live = ContextVar("live_events", default=False)
_answer = ContextVar("answer_stream", default=False)
# 跨副本/多线程 start() 并发上限检查使用的 PostgreSQL 事务级 advisory lock 键。
_START_ADVISORY_LOCK_KEY = 0x54363952
_cancel_signals = {}
_ACTIVE_RUN_STATUSES = ("running", "cancel_requested")


@dataclass(frozen=True)
class TaskLease:
    """数据库后台任务租约令牌（fencing token）。"""

    task_id: str
    owner: str
    token: str
    attempt: int
    expires_at: datetime


class LeaseLostError(RuntimeError):
    """任务租约已失效或已被其他 worker 接管，禁止继续写入状态或结果。"""


def make_worker_id(prefix: str = "worker") -> str:
    host = socket.gethostname()[:32] or "host"
    return f"{prefix}:{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def make_lease_token(attempt: int) -> str:
    return f"a{attempt}-{uuid.uuid4().hex[:24]}"


def make_run_idempotency_key(session_id: str, request_id: str | None) -> str | None:
    if not request_id:
        return None
    return f"run:{session_id}:{request_id}"


def make_message_idempotency_key(run_id: str | None, role: str) -> str | None:
    if not run_id:
        return None
    return f"msg:{run_id}:{role}"


def _dialect_name(db) -> str:
    bind = db.get_bind()
    return getattr(getattr(bind, "dialect", None), "name", "")


def lock_run_row(db, run_id: str, tenant_id: str):
    """锁定指定 AgentRun 行并刷新为已提交最新状态（PG 用 FOR UPDATE，SQLite 用先写抢占写锁）。"""
    from sqlalchemy import update

    from app.db.models import AgentRun, _now

    dialect = _dialect_name(db)
    if dialect == "postgresql":
        return (
            db.query(AgentRun)
            .filter_by(id=run_id, tenant_id=tenant_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
    if dialect == "sqlite":
        updated = db.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.tenant_id == tenant_id)
            .values(updated_at=_now())
        ).rowcount
        if updated == 0:
            return None
    return db.query(AgentRun).filter_by(id=run_id, tenant_id=tenant_id).populate_existing().first()


class RunCancelled(RuntimeError):
    pass


def check_lease():
    recorder = current_run()
    lost = getattr(recorder, "lease_lost", None)
    if lost is not None and lost.is_set():
        raise LeaseLostError(f"AgentRun 租约已失效: run_id={getattr(recorder, 'id', '')}")


def check_cancelled():
    check_lease()
    recorder = current_run()
    signal = getattr(recorder, "cancel_signal", None)
    if signal is not None:
        poll = getattr(recorder, "poll_cancel", None)
        if not signal.is_set() and poll is not None:
            poll()
        if signal.is_set():
            raise RunCancelled("用户已停止本次运行")


def run_cancelable(operation):
    """等待当前运行的异步网络操作，取消时等待它退出并关闭其响应。"""
    async def wait():
        check_cancelled()
        task = asyncio.create_task(operation())
        try:
            while not task.done():
                check_cancelled()
                tool_time_left()
                await asyncio.wait({task}, timeout=0.1)
            result = task.result()
            check_cancelled()
            return result
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return asyncio.run(wait())


def request_cancel(db, run):
    from sqlalchemy import update

    from app.db.base import run_with_sqlite_retry
    from app.db.models import AgentRun, _now

    if run.status != "running":
        return run.status
    def _apply():
        try:
            db.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == run.id,
                    AgentRun.tenant_id == run.tenant_id,
                    AgentRun.status == "running",
                )
                .values(status="cancel_requested", updated_at=_now())
            )
            db.commit()
        except Exception:
            db.rollback()
            raise

    run_with_sqlite_retry(_apply)
    db.refresh(run)
    signal = _cancel_signals.get(run.id)
    if signal is not None and run.status == "cancel_requested":
        signal.set()
    return run.status


def current_run():
    return _current.get()


@contextmanager
def live_events():
    token = _live.set(True)
    try:
        yield
    finally:
        _live.reset(token)


@contextmanager
def answer_stream():
    token = _answer.set(True)
    try:
        yield
    finally:
        _answer.reset(token)


def live_model_stream():
    return _live.get() and _agent.get() in ("main", "support")


def push_live(event):
    if not _live.get() or (event["type"] in ("token", "answer_reset") and _agent.get() not in ("main", "support")):
        return
    from langgraph.config import get_stream_writer

    get_stream_writer()({**event, "agent": _agent.get()})


def push_answer(delta):
    if _answer.get():
        push_live({"type": "token", "delta": delta})


@contextmanager
def agent_scope(name):
    token = _agent.set(name)
    try:
        yield
    finally:
        _agent.reset(token)


@contextmanager
def capture_run(recorder):
    token = _current.set(recorder)
    try:
        yield
    finally:
        _current.reset(token)


@contextmanager
def tool_deadline(seconds):
    token = _deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


def tool_time_left():
    deadline = _deadline.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("只读工具超过时间预算")
    return remaining


def emit(kind, detail):
    recorder = current_run()
    if recorder:
        try:
            recorder.add(kind, {**detail, "agent": _agent.get()})
        except LeaseLostError:
            if getattr(recorder, "lease_lost", None) is not None:
                recorder.lease_lost.set()
            logger.warning(
                "run_event_rejected_lease_lost run_id=%s kind=%s owner=%s",
                getattr(recorder, "id", "offline"),
                kind,
                getattr(recorder, "lease_owner", None),
            )
        except Exception as exc:
            # 附加追踪故障不改变业务结果；异常原文可能含SQL参数/正文，只记录类型。
            logger.warning(
                "run_event_write_failed run_id=%s kind=%s error_type=%s",
                getattr(recorder, "id", "offline"),
                kind,
                type(exc).__name__,
            )


class RunRecorder:
    def __init__(
        self,
        run_id,
        tenant_id,
        *,
        lease_owner: str | None = None,
        lease_token: str | None = None,
        attempt: int | None = None,
        session_factory=None,
    ):
        self.id, self.tenant_id, self.finished = run_id, tenant_id, False
        self.lease_owner = lease_owner
        self.lease_token = lease_token
        self.attempt = attempt
        self._session_factory = session_factory
        self.lease_lost = threading.Event()
        self.cancel_signal = _cancel_signals.setdefault(run_id, threading.Event())
        self._next_cancel_poll = 0.0

    def poll_cancel(self):
        now = time.monotonic()
        if now < self._next_cancel_poll:
            return
        self._next_cancel_poll = now + 0.25
        from sqlalchemy import select

        from app.db.base import SessionLocal
        from app.db.models import AgentRun

        factory = self._session_factory or SessionLocal
        with factory() as db:
            if _dialect_name(db) == "postgresql":
                status = db.scalar(select(AgentRun.status).where(AgentRun.id == self.id))
                if status == "cancel_requested":
                    self.cancel_signal.set()

    @property
    def has_lease(self) -> bool:
        return self.lease_owner is not None or self.lease_token is not None

    @classmethod
    def start(
        cls,
        container,
        tenant_id,
        session_id,
        question,
        request_id=None,
        workflow="auto",
        *,
        background=False,
        queued=False,
        idempotency_key=None,
        lease_owner=None,
        lease_seconds=None,
        session_factory=None,
    ):
        from fastapi import HTTPException
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        from app.db.base import SessionLocal, run_with_sqlite_retry
        from app.db.models import AgentRun, AgentRunEvent, ChatSession, _now

        settings = container.settings
        factory = session_factory or SessionLocal
        config_keys = (
            "agent_mode",
            "llm_provider",
            "chat_model",
            "embedding_provider",
            "embedding_model",
            "embedding_dim",
            "rerank_model",
            "retrieval_mode",
            "tool_timeout_seconds",
            "model_strict",
            "vector_store_backend",
            "metrics_provider",
            "drill_enabled",
            "mcp_enabled",
            "retrieval_subagent_enabled",
        )
        run_id = uuid.uuid4().hex
        resolved_idem_key = idempotency_key or make_run_idempotency_key(session_id, request_id)
        initial_owner = None
        initial_token = None
        initial_attempt = 0
        initial_heartbeat = None
        initial_expires = None
        if background and not queued:
            # 直接手工驱动的后台运行（未通过队列 enqueue）先绑定初始租约，防止后台轮询线程并发抢占。
            now = _now()
            ttl = float(lease_seconds if lease_seconds is not None else getattr(settings, "task_lease_seconds", 30.0))
            initial_attempt = 1
            initial_owner = lease_owner or make_worker_id("inline")
            initial_token = make_lease_token(initial_attempt)
            initial_heartbeat = now
            initial_expires = now + timedelta(seconds=ttl)

        recorder = cls(
            run_id,
            tenant_id,
            lease_owner=initial_owner,
            lease_token=initial_token,
            attempt=initial_attempt if initial_attempt else None,
            session_factory=session_factory,
        )
        root = Path(__file__).resolve().parents[2]
        sources = (
            "app/agent/tool_loop.py",
            "app/agent/tools.py",
            "app/agent/nodes.py",
            "app/services/model_api.py",
            "app/services/metrics_service.py",
            "app/services/log_service.py",
            "app/mcp_server.py",
            "app/services/mcp_client.py",
            "app/agent/retrieval_agent.py",
        )
        run_payload = {
            "question": question,
            "workflow": workflow,
            **({"background": True} if background else {}),
            "config": {k: getattr(settings, k, None) for k in config_keys},
            "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sources},
            "backends": {
                k: type(getattr(container, k, None)).__name__
                for k in ("llm", "embedder", "vector_store", "cache")
            },
            "max_tool_calls": getattr(settings, "max_tool_calls", 5),
            "events": [],
        }

        def _insert():
            with factory() as db:
                dialect = _dialect_name(db)
                if dialect == "postgresql":
                    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _START_ADVISORY_LOCK_KEY})
                elif dialect == "sqlite":
                    db.execute(text("UPDATE tenants SET name = name WHERE id = :tid"), {"tid": tenant_id})

                active = (
                    db.query(AgentRun)
                    .join(ChatSession, AgentRun.session_id == ChatSession.id)
                    .filter(AgentRun.status.in_(["running", "cancel_requested"]), ChatSession.deleted_at.is_(None))
                )
                if active.filter(AgentRun.session_id == session_id).first():
                    _cancel_signals.pop(recorder.id, None)
                    raise HTTPException(409, "本会话已有执行中的请求，请查看当前运行记录")
                if active.count() >= getattr(settings, "max_concurrent_runs", 1):
                    _cancel_signals.pop(recorder.id, None)
                    raise HTTPException(429, "当前分析任务已满，请稍后重试；单实例演示默认只执行一项分析")
                try:
                    db.add(
                        AgentRun(
                            id=recorder.id,
                            tenant_id=tenant_id,
                            session_id=session_id,
                            mode=getattr(settings, "agent_mode", "rule"),
                            status="running",
                            event_seq=1,
                            request_id=request_id,
                            idempotency_key=resolved_idem_key,
                            lease_owner=initial_owner,
                            lease_token=initial_token,
                            attempt=initial_attempt,
                            heartbeat_at=initial_heartbeat,
                            lease_expires_at=initial_expires,
                            data=copy.deepcopy(run_payload),
                        )
                    )
                    db.flush()
                    db.add(
                        AgentRunEvent(
                            run_id=recorder.id,
                            tenant_id=tenant_id,
                            seq=1,
                            kind="started",
                            detail={"request_id": request_id, "workflow": workflow},
                        )
                    )
                    db.commit()
                except IntegrityError as exc:
                    db.rollback()
                    _cancel_signals.pop(recorder.id, None)
                    raise HTTPException(
                        409, "本会话已有执行中的请求，或这个请求编号已经使用；请查看当前运行记录"
                    ) from exc

        run_with_sqlite_retry(_insert)
        return recorder

    def _update(self, event=None, result=None):
        from sqlalchemy import update

        from app.db.base import SessionLocal, run_with_sqlite_retry
        from app.db.models import AgentRun, AgentRunEvent, _now

        if self.has_lease and self.lease_lost.is_set():
            raise LeaseLostError(f"AgentRun 租约已失效: run_id={self.id} owner={self.lease_owner}")

        factory = self._session_factory or SessionLocal

        def _apply():
            with factory() as db:
                row = lock_run_row(db, self.id, self.tenant_id)
                if row is None:
                    raise LookupError(f"AgentRun 不存在: {self.id}")
                now = _now()
                if self.has_lease:
                    if (
                        row.lease_expires_at is None
                        or row.lease_expires_at <= now
                        or row.lease_owner != self.lease_owner
                        or row.lease_token != self.lease_token
                        or row.status not in _ACTIVE_RUN_STATUSES
                    ):
                        db.rollback()
                        self.lease_lost.set()
                        raise LeaseLostError(
                            f"AgentRun 租约校验失败: run_id={self.id} expected=({self.lease_owner}, {self.lease_token}) "
                            f"actual=({row.lease_owner}, {row.lease_token}, status={row.status})"
                        )
                assigned_seq = None
                values = {"updated_at": now}
                if event:
                    assigned_seq = (row.event_seq or 0) + 1
                    values["event_seq"] = assigned_seq
                if result is not None:
                    data = copy.deepcopy(row.data)
                    cancelled = self.cancel_signal.is_set() or row.status == "cancel_requested"
                    if cancelled:
                        reason = result.get("end_reason") if result.get("status") == "cancelled" else None
                        data["result"] = {"status": "cancelled", "end_reason": reason or "user_cancelled"}
                    else:
                        data["result"] = {**data.get("result", {}), **result}
                    if cancelled and event and event["kind"] == "finished":
                        event["detail"] = {"status": "cancelled"}
                    new_status = (
                        "cancelled"
                        if cancelled
                        else result.get("approval_status")
                        or result.get("status")
                        or ("completed" if result.get("end_reason") == "answered" else "failed")
                    )
                    values["status"] = new_status
                    values["data"] = data
                    if self.has_lease:
                        values["lease_expires_at"] = None
                        values["heartbeat_at"] = now

                if self.has_lease:
                    updated = db.execute(
                        update(AgentRun)
                        .where(
                            AgentRun.id == self.id,
                            AgentRun.tenant_id == self.tenant_id,
                            AgentRun.lease_owner == self.lease_owner,
                            AgentRun.lease_token == self.lease_token,
                            AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
                        )
                        .values(**values)
                    ).rowcount
                    if updated != 1:
                        db.rollback()
                        self.lease_lost.set()
                        raise LeaseLostError(
                            f"AgentRun 条件更新被拒绝: run_id={self.id} owner={self.lease_owner} token={self.lease_token}"
                        )
                else:
                    if "event_seq" in values:
                        row.event_seq = values["event_seq"]
                    if "status" in values:
                        row.status = values["status"]
                    if "data" in values:
                        row.data = values["data"]
                    row.updated_at = now

                if event:
                    db.add(
                        AgentRunEvent(
                            run_id=self.id,
                            tenant_id=self.tenant_id,
                            seq=assigned_seq,
                            kind=event["kind"],
                            detail=event["detail"],
                        )
                    )
                db.commit()
                return assigned_seq

        return run_with_sqlite_retry(_apply)

    def add(self, kind, detail):
        return self._update(event={"kind": kind, "detail": detail})

    def finish(self, result):
        self._update(
            event={"kind": "finished", "detail": {"status": result.get("status") or result.get("end_reason") or "finished"}},
            result=result,
        )
        self.finished = True
        _cancel_signals.pop(self.id, None)


def get_run(db, tenant_id, run_id, user_id):
    from app.db.models import AgentRun, ChatSession

    return (
        db.query(AgentRun)
        .join(ChatSession, AgentRun.session_id == ChatSession.id)
        .filter(
            AgentRun.id == run_id,
            AgentRun.tenant_id == tenant_id,
            ChatSession.tenant_id == tenant_id,
            ChatSession.user_id == user_id,
            ChatSession.deleted_at.is_(None),
        )
        .first()
    )


def serialize_run(db, run):
    from app.db.models import AgentRunEvent

    events = (
        db.query(AgentRunEvent)
        .filter_by(run_id=run.id, tenant_id=run.tenant_id)
        .filter(AgentRunEvent.kind != "stream")
        .order_by(AgentRunEvent.seq, AgentRunEvent.id)
        .all()
    )
    return {
        "run_id": run.id,
        "session_id": run.session_id,
        "mode": run.mode,
        "status": run.status,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "data": {
            **{key: value for key, value in run.data.items() if key != "background_input"},
            "events": [
                *run.data.get("events", []),
                *[
                    {"id": e.id, "seq": e.seq, "at": e.created_at.isoformat(), "kind": e.kind, "detail": e.detail}
                    for e in events
                ],
            ],
        },
    }

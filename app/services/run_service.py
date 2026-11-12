"""按 run_id 记录运行事件；上下文跟随执行线程，迟到的只读请求用量仍可追加。"""

import copy
import hashlib
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

logger = logging.getLogger(__name__)

_current = ContextVar("agent_run", default=None)
_deadline = ContextVar("tool_deadline", default=None)
_agent = ContextVar("agent_name", default="main")
_live = ContextVar("live_events", default=False)
_answer = ContextVar("answer_stream", default=False)
# shortcut: 单API进程串行合并JSON事件；多实例时升级为数据库原子追加。
_write_lock = threading.Lock()
_cancel_signals = {}


class RunCancelled(RuntimeError):
    pass


def check_cancelled():
    recorder = current_run()
    signal = getattr(recorder, "cancel_signal", None)
    if signal and signal.is_set():
        raise RunCancelled("用户已停止本次运行")


def request_cancel(db, run):
    if run.status != "running":
        return run.status
    signal = _cancel_signals.get(run.id)
    if signal:
        signal.set()
    run.status = "cancel_requested"
    db.commit()
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
        except Exception as exc:
            # 附加追踪故障不改变业务结果；异常原文可能含SQL参数/正文，只记录类型。
            logger.warning(
                "run_event_write_failed run_id=%s kind=%s error_type=%s",
                getattr(recorder, "id", "offline"),
                kind,
                type(exc).__name__,
            )


class RunRecorder:
    def __init__(self, run_id, tenant_id):
        self.id, self.tenant_id, self.finished = run_id, tenant_id, False
        self.cancel_signal = _cancel_signals.setdefault(run_id, threading.Event())

    @classmethod
    def start(cls, container, tenant_id, session_id, question, request_id=None, workflow="auto"):
        from app.db.base import SessionLocal
        from app.db.models import AgentRun

        settings = container.settings
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
            "qdrant_mode",
            "metrics_provider",
            "mcp_enabled",
            "retrieval_subagent_enabled",
        )
        recorder = cls(uuid.uuid4().hex, tenant_id)
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
        with _write_lock, SessionLocal() as db:
            from fastapi import HTTPException

            from app.db.models import ChatSession

            active = (
                db.query(AgentRun)
                .join(ChatSession)
                .filter(AgentRun.status.in_(["running", "cancel_requested"]), ChatSession.deleted_at.is_(None))
            )
            if active.filter(AgentRun.session_id == session_id).first():
                _cancel_signals.pop(recorder.id, None)
                raise HTTPException(409, "本会话已有执行中的请求，请查看当前运行记录")
            if active.count() >= getattr(settings, "max_concurrent_runs", 1):
                _cancel_signals.pop(recorder.id, None)
                raise HTTPException(429, "当前分析任务已满，请稍后重试；单实例演示默认只执行一项分析")
            db.add(
                AgentRun(
                    id=recorder.id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    mode=getattr(settings, "agent_mode", "rule"),
                    status="running",
                    request_id=request_id,
                    data={
                        "question": question,
                        "workflow": workflow,
                        "config": {k: getattr(settings, k, None) for k in config_keys},
                        "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sources},
                        "backends": {
                            k: type(getattr(container, k, None)).__name__
                            for k in ("llm", "embedder", "vector_store", "cache")
                        },
                        "max_tool_calls": getattr(settings, "max_tool_calls", 5),
                        "events": [],
                    },
                )
            )
            try:
                db.commit()
            except Exception as exc:
                from sqlalchemy.exc import IntegrityError

                if not isinstance(exc, IntegrityError):
                    raise
                _cancel_signals.pop(recorder.id, None)
                from fastapi import HTTPException

                raise HTTPException(409, "本会话已有执行中的请求，或这个请求编号已经使用；请查看当前运行记录") from exc
        recorder.add("started", {"request_id": request_id, "workflow": workflow})
        return recorder

    def _update(self, event=None, result=None):
        from sqlalchemy import update

        from app.db.base import SessionLocal
        from app.db.models import AgentRun, AgentRunEvent, _now

        with _write_lock, SessionLocal() as db:
            if event:
                db.add(
                    AgentRunEvent(run_id=self.id, tenant_id=self.tenant_id, kind=event["kind"], detail=event["detail"])
                )
                db.execute(
                    update(AgentRun)
                    .where(AgentRun.id == self.id, AgentRun.tenant_id == self.tenant_id)
                    .values(updated_at=_now())
                )
            if result is not None:
                row = db.query(AgentRun).filter_by(id=self.id, tenant_id=self.tenant_id).one()
                data = copy.deepcopy(row.data)
                data["result"] = {**data.get("result", {}), **result}
                row.status = (
                    "cancelled"
                    if self.cancel_signal.is_set()
                    else result.get("approval_status")
                    or result.get("status")
                    or ("completed" if result.get("end_reason") == "answered" else "failed")
                )
                row.data, row.updated_at = data, _now()
            db.commit()

    def add(self, kind, detail):
        self._update(event={"kind": kind, "detail": detail})

    def finish(self, result):
        self.add("finished", {"status": result.get("status") or result.get("end_reason") or "finished"})
        self._update(result=result)
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

    events = db.query(AgentRunEvent).filter_by(run_id=run.id, tenant_id=run.tenant_id).order_by(AgentRunEvent.id).all()
    return {
        "run_id": run.id,
        "session_id": run.session_id,
        "mode": run.mode,
        "status": run.status,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "data": {
            **run.data,
            "events": [
                *run.data.get("events", []),
                *[{"id": e.id, "at": e.created_at.isoformat(), "kind": e.kind, "detail": e.detail} for e in events],
            ],
        },
    }

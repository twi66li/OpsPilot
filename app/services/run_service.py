"""按 run_id 记录运行事件；上下文跟随执行线程，迟到的只读请求用量仍可追加。"""
import copy
import hashlib
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_current = ContextVar("agent_run", default=None)
_deadline = ContextVar("tool_deadline", default=None)
_agent = ContextVar("agent_name", default="main")
# shortcut: 单API进程串行合并JSON事件；多实例时升级为数据库原子追加。
_write_lock = threading.Lock()


def current_run():
    return _current.get()


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
            logger.warning("run_event_write_failed run_id=%s kind=%s error_type=%s",
                           getattr(recorder, "id", "offline"), kind, type(exc).__name__)


class RunRecorder:
    def __init__(self, run_id, tenant_id):
        self.id, self.tenant_id, self.finished = run_id, tenant_id, False

    @classmethod
    def start(cls, container, tenant_id, session_id, question):
        from app.db.base import SessionLocal
        from app.db.models import AgentRun

        settings = container.settings
        config_keys = ("agent_mode", "llm_provider", "chat_model", "embedding_provider", "embedding_model", "embedding_dim", "rerank_model", "retrieval_mode", "tool_timeout_seconds", "model_strict", "qdrant_mode", "metrics_provider", "mcp_enabled", "retrieval_subagent_enabled")
        recorder = cls(uuid.uuid4().hex, tenant_id)
        root = Path(__file__).resolve().parents[2]
        sources = ("app/agent/tool_loop.py", "app/agent/tools.py", "app/agent/nodes.py", "app/services/model_api.py",
                   "app/services/metrics_service.py", "app/services/log_service.py", "app/mcp_server.py",
                   "app/services/mcp_client.py", "app/agent/retrieval_agent.py")
        with _write_lock, SessionLocal() as db:
            db.add(AgentRun(
                id=recorder.id, tenant_id=tenant_id, session_id=session_id,
                mode=getattr(settings, "agent_mode", "rule"), status="running",
                data={"question": question, "config": {k: getattr(settings, k, None) for k in config_keys},
                      "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sources},
                      "backends": {k: type(getattr(container, k, None)).__name__ for k in ("llm", "embedder", "vector_store", "cache")},
                      "max_tool_calls": getattr(settings, "max_tool_calls", 5), "events": []},
            ))
            db.commit()
        return recorder

    def _update(self, event=None, result=None):
        from app.db.base import SessionLocal
        from app.db.models import AgentRun

        with _write_lock, SessionLocal() as db:
            row = db.query(AgentRun).filter(AgentRun.id == self.id, AgentRun.tenant_id == self.tenant_id).one()
            data = copy.deepcopy(row.data)
            if event:
                data["events"].append({"at": datetime.now(timezone.utc).isoformat(), **event})
            if result is not None:
                data["result"] = result
                row.status = result.get("approval_status") or result.get("status", "failed")
            row.data = data
            row.updated_at = datetime.now(timezone.utc)
            db.commit()

    def add(self, kind, detail):
        self._update(event={"kind": kind, "detail": detail})

    def finish(self, result):
        self._update(result=result)
        self.finished = True


def get_run(db, tenant_id, run_id, user_id):
    from app.db.models import AgentRun, ChatSession

    return db.query(AgentRun).join(ChatSession, AgentRun.session_id == ChatSession.id).filter(
        AgentRun.id == run_id, AgentRun.tenant_id == tenant_id,
        ChatSession.tenant_id == tenant_id, ChatSession.user_id == user_id,
    ).first()

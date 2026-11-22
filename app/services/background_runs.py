"""基于数据库事务租约的后台调查任务队列：领取、心跳续约、过期重领与过期 worker 拒绝。"""

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import base as db_base
from app.db.base import SessionLocal, run_with_sqlite_retry
from app.db.models import AgentRun, AgentRunEvent, ChatSession, SupportCase, User, _now
from app.services import agent_service
from app.services.case_service import assert_editable
from app.services.run_service import (
    LeaseLostError,
    RunCancelled,
    RunRecorder,
    TaskLease,
    _dialect_name,
    capture_run,
    check_cancelled,
    check_lease,
    get_run,
    live_events,
    make_lease_token,
    make_run_idempotency_key,
    make_worker_id,
)
from app.services.support_service import redact

logger = logging.getLogger(__name__)
_INITIAL_SESSION_LOCAL = SessionLocal
_worker: ThreadPoolExecutor | None = None
_runtimes: dict[int, "_WorkerRuntime"] = {}
_runtime_lock = threading.Lock()
_waiter_lock = threading.Lock()
_waiters: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}
ACTIVE = ("running", "cancel_requested")


@dataclass
class _WorkerRuntime:
    executor: ThreadPoolExecutor
    container: object
    worker_id: str
    max_workers: int
    stop_event: threading.Event = field(default_factory=threading.Event)
    wake_event: threading.Event = field(default_factory=threading.Event)
    slot_lock: threading.Lock = field(default_factory=threading.Lock)
    inflight: int = 0
    poll_thread: threading.Thread | None = None


def _resolve_session_factory(session_factory=None):
    if session_factory is not None:
        return session_factory
    if SessionLocal is not _INITIAL_SESSION_LOCAL:
        return SessionLocal
    return db_base.SessionLocal


def _notify(run_id: str) -> None:
    with _waiter_lock:
        targets = list(_waiters.get(run_id, ()))
    for loop, event in targets:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass


def _current_runtime() -> _WorkerRuntime | None:
    worker = _worker
    if worker is None:
        return None
    with _runtime_lock:
        return _runtimes.get(id(worker))


def claim_next_run(
    worker_id: str,
    *,
    lease_seconds: float | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
    session_factory=None,
) -> TaskLease | None:
    """在数据库事务内领取一条待执行或租约已过期的后台调查任务。

    PostgreSQL 使用 SELECT ... FOR UPDATE SKIP LOCKED 避免多 worker 阻塞与冲突；
    SQLite 使用事务串行 + 条件 UPDATE（CAS）保证同一任务只被唯一 worker 领取。
    """
    factory = _resolve_session_factory(session_factory)
    ttl = float(lease_seconds if lease_seconds is not None else get_settings().task_lease_seconds)

    def _claim() -> TaskLease | None:
        ts = now or _now()
        expires_at = ts + timedelta(seconds=ttl)
        conditions = [
            AgentRun.status.in_(ACTIVE),
            AgentRun.data["background"].as_boolean().is_(True),
            or_(
                AgentRun.lease_owner.is_(None),
                AgentRun.lease_expires_at.is_(None),
                AgentRun.lease_expires_at <= ts,
            ),
        ]
        if run_id is not None:
            conditions.append(AgentRun.id == run_id)

        with factory() as db:
            dialect = _dialect_name(db)
            if dialect == "postgresql":
                stmt = (
                    select(AgentRun)
                    .where(*conditions)
                    .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                row = db.scalars(stmt).first()
                if row is None:
                    return None
                next_attempt = (row.attempt or 0) + 1
                token = make_lease_token(next_attempt)
                row.attempt = next_attempt
                row.lease_owner = worker_id
                row.lease_token = token
                row.heartbeat_at = ts
                row.lease_expires_at = expires_at
                row.updated_at = ts
                db.commit()
                return TaskLease(
                    task_id=row.id,
                    owner=worker_id,
                    token=token,
                    attempt=next_attempt,
                    expires_at=expires_at,
                )

            candidates = db.execute(
                select(AgentRun.id, AgentRun.attempt)
                .where(*conditions)
                .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
                .limit(1 if run_id is not None else 8)
            ).all()
            for cand_id, cand_attempt in candidates:
                current_attempt = cand_attempt or 0
                next_attempt = current_attempt + 1
                token = make_lease_token(next_attempt)
                updated = db.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.id == cand_id,
                        AgentRun.status.in_(ACTIVE),
                        AgentRun.attempt == current_attempt,
                        or_(
                            AgentRun.lease_owner.is_(None),
                            AgentRun.lease_expires_at.is_(None),
                            AgentRun.lease_expires_at <= ts,
                        ),
                    )
                    .values(
                        attempt=next_attempt,
                        lease_owner=worker_id,
                        lease_token=token,
                        heartbeat_at=ts,
                        lease_expires_at=expires_at,
                        updated_at=ts,
                    )
                ).rowcount
                if updated == 1:
                    db.commit()
                    return TaskLease(
                        task_id=cand_id,
                        owner=worker_id,
                        token=token,
                        attempt=next_attempt,
                        expires_at=expires_at,
                    )
                db.rollback()
            return None

    return run_with_sqlite_retry(_claim)


def claim_run_by_id(
    run_id: str,
    worker_id: str,
    *,
    lease_seconds: float | None = None,
    now: datetime | None = None,
    session_factory=None,
) -> TaskLease | None:
    return claim_next_run(
        worker_id,
        lease_seconds=lease_seconds,
        now=now,
        run_id=run_id,
        session_factory=session_factory,
    )


def renew_run_lease(
    run_id: str,
    lease_owner: str,
    lease_token: str,
    *,
    lease_seconds: float | None = None,
    now: datetime | None = None,
    session_factory=None,
) -> bool:
    """按 (lease_owner, lease_token) 条件续约心跳，过期/易主返回 False。"""
    factory = _resolve_session_factory(session_factory)
    ttl = float(lease_seconds if lease_seconds is not None else get_settings().task_lease_seconds)

    def _renew() -> bool:
        ts = now or _now()
        expires_at = ts + timedelta(seconds=ttl)
        with factory() as db:
            updated = db.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.status.in_(ACTIVE),
                    AgentRun.lease_owner == lease_owner,
                    AgentRun.lease_token == lease_token,
                )
                .values(heartbeat_at=ts, lease_expires_at=expires_at, updated_at=ts)
            ).rowcount
            db.commit()
            return updated == 1

    return run_with_sqlite_retry(_renew)


def finish_run_with_lease(
    run_id: str,
    tenant_id: str,
    lease: TaskLease,
    result: dict,
    *,
    session_factory=None,
) -> bool:
    """携带租约令牌提交调查任务终态；若租约已失效则返回 False（绝不覆盖新持有人结果）。"""
    recorder = RunRecorder(
        run_id,
        tenant_id,
        lease_owner=lease.owner,
        lease_token=lease.token,
        attempt=lease.attempt,
        session_factory=session_factory,
    )
    try:
        recorder.finish(result)
        return True
    except LeaseLostError:
        return False


@contextmanager
def _lease_heartbeat(
    lease: TaskLease,
    *,
    lease_seconds: float,
    heartbeat_interval: float,
    on_lost,
    is_finished=lambda: False,
    session_factory=None,
):
    stop = threading.Event()
    interval = max(0.02, min(float(heartbeat_interval), max(0.02, float(lease_seconds) / 3.0)))

    def _loop():
        while not stop.wait(interval):
            if is_finished():
                return
            try:
                ok = renew_run_lease(
                    lease.task_id,
                    lease.owner,
                    lease.token,
                    lease_seconds=lease_seconds,
                    session_factory=session_factory,
                )
                if not ok:
                    if not is_finished():
                        on_lost()
                    return
            except Exception as exc:
                logger.warning(
                    "background_run_heartbeat_failed run_id=%s owner=%s error_type=%s",
                    lease.task_id,
                    lease.owner,
                    type(exc).__name__,
                )

    thread = threading.Thread(
        target=_loop,
        name=f"run-heartbeat-{lease.task_id[:8]}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2.0)


def _run_claimed(runtime: _WorkerRuntime, lease: TaskLease) -> None:
    try:
        execute(
            runtime.container,
            lease.task_id,
            lease=lease,
            lease_seconds=runtime.container.settings.task_lease_seconds,
        )
    finally:
        with runtime.slot_lock:
            runtime.inflight = max(0, runtime.inflight - 1)
        runtime.wake_event.set()


def _dispatch_available(runtime: _WorkerRuntime) -> int:
    dispatched = 0
    lease_seconds = runtime.container.settings.task_lease_seconds
    while not runtime.stop_event.is_set():
        with runtime.slot_lock:
            if runtime.inflight >= runtime.max_workers:
                break
            runtime.inflight += 1
        try:
            lease = claim_next_run(runtime.worker_id, lease_seconds=lease_seconds)
        except Exception as exc:
            with runtime.slot_lock:
                runtime.inflight = max(0, runtime.inflight - 1)
            logger.warning("background_run_claim_failed worker=%s error_type=%s", runtime.worker_id, type(exc).__name__)
            break
        if lease is None:
            with runtime.slot_lock:
                runtime.inflight = max(0, runtime.inflight - 1)
            break
        try:
            runtime.executor.submit(_run_claimed, runtime, lease)
            dispatched += 1
        except RuntimeError:
            with runtime.slot_lock:
                runtime.inflight = max(0, runtime.inflight - 1)
            break
    return dispatched


def start_worker(container, *, worker_id: str | None = None):
    global _worker
    max_workers = max(1, int(container.settings.max_concurrent_runs))
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="diagnosis")
    runtime = _WorkerRuntime(
        executor=executor,
        container=container,
        worker_id=worker_id or make_worker_id("diagnosis"),
        max_workers=max_workers,
    )
    with _runtime_lock:
        _worker = executor
        _runtimes[id(executor)] = runtime

    # 启动后立即领取一轮待执行或租约已过期的后台任务，再进入周期轮询。
    _dispatch_available(runtime)

    def _poll():
        while not runtime.stop_event.is_set():
            poll_interval = max(0.05, float(getattr(container.settings, "task_poll_interval_seconds", 0.5)))
            runtime.wake_event.wait(timeout=poll_interval)
            runtime.wake_event.clear()
            if runtime.stop_event.is_set():
                break
            _dispatch_available(runtime)

    runtime.poll_thread = threading.Thread(
        target=_poll,
        name=f"diagnosis-poller-{runtime.worker_id[-8:]}",
        daemon=True,
    )
    runtime.poll_thread.start()
    return runtime.worker_id


def stop_worker():
    global _worker
    with _runtime_lock:
        worker = _worker
        _worker = None
        runtime = _runtimes.pop(id(worker), None) if worker is not None else None
    if runtime is not None:
        runtime.stop_event.set()
        runtime.wake_event.set()
        if runtime.poll_thread is not None and runtime.poll_thread.is_alive():
            runtime.poll_thread.join(timeout=5.0)
    if worker is not None:
        worker.shutdown(wait=True)


def _find_idempotent_run(db, tenant_id: str, session_id: str, request_id: str | None, idempotency_key: str | None):
    if idempotency_key:
        run = db.query(AgentRun).filter_by(tenant_id=tenant_id, idempotency_key=idempotency_key).first()
        if run is not None:
            return run
    if request_id:
        return (
            db.query(AgentRun)
            .filter_by(tenant_id=tenant_id, session_id=session_id, request_id=request_id)
            .first()
        )
    return None


def enqueue(container, user, body, *, idempotency_key: str | None = None):
    if _worker is None:
        raise HTTPException(503, "调查后台尚未就绪")
    factory = _resolve_session_factory()

    def _is_personal():
        with factory() as db:
            return db.get(SupportCase, body.session_id) is not None

    personal = run_with_sqlite_retry(_is_personal)
    question = redact(body.message)["text"] if personal else body.message
    resolved_key = idempotency_key or make_run_idempotency_key(body.session_id, body.request_id)

    def _validate_existing(existing: AgentRun) -> str:
        if (
            existing.data.get("question") not in (body.message, question)
            or existing.data.get("workflow", "auto") != body.workflow
        ):
            raise HTTPException(409, "同一请求编号不能用于不同问题或工作流")
        return existing.id

    def _lookup_existing():
        with factory() as db:
            return _find_idempotent_run(db, user.tenant_id, body.session_id, body.request_id, resolved_key)

    if resolved_key or body.request_id:
        existing = run_with_sqlite_retry(_lookup_existing)
        if existing is not None:
            run_id = _validate_existing(existing)
            runtime = _current_runtime()
            if runtime is not None:
                runtime.wake_event.set()
                _dispatch_available(runtime)
            return run_id

    try:
        recorder = RunRecorder.start(
            container,
            user.tenant_id,
            body.session_id,
            question,
            body.request_id,
            body.workflow,
            background=True,
            queued=True,
            idempotency_key=resolved_key,
        )
        run_id = recorder.id
    except HTTPException as exc:
        if exc.status_code == 409 and (resolved_key or body.request_id):
            existing = run_with_sqlite_retry(_lookup_existing)
            if existing is not None and existing.data.get("background"):
                run_id = _validate_existing(existing)
            else:
                raise
        else:
            raise

    runtime = _current_runtime()
    if runtime is not None:
        runtime.wake_event.set()
        _dispatch_available(runtime)
    else:
        _worker.submit(execute, container, run_id)
    return run_id


def run_once(
    container,
    *,
    worker_id: str | None = None,
    run_id: str | None = None,
    lease_seconds: float | None = None,
    now: datetime | None = None,
    session_factory=None,
) -> str | None:
    """单次领取并执行一条后台调查任务（供测试及独立 worker 驱动）。"""
    owner = worker_id or make_worker_id("diagnosis")
    lease = claim_next_run(
        owner,
        lease_seconds=lease_seconds,
        now=now,
        run_id=run_id,
        session_factory=session_factory,
    )
    if lease is None:
        return None
    ok = execute(
        container,
        lease.task_id,
        lease=lease,
        lease_seconds=lease_seconds,
        session_factory=session_factory,
    )
    return lease.task_id if ok else None


def execute(
    container,
    run_id,
    *,
    lease: TaskLease | None = None,
    worker_id: str | None = None,
    lease_seconds: float | None = None,
    session_factory=None,
) -> bool:
    factory = _resolve_session_factory(session_factory)
    ttl = float(lease_seconds if lease_seconds is not None else getattr(container.settings, "task_lease_seconds", 30.0))
    heartbeat_interval = float(getattr(container.settings, "task_heartbeat_interval_seconds", 10.0))

    if lease is None:
        lease = claim_run_by_id(
            run_id,
            worker_id or make_worker_id("diagnosis"),
            lease_seconds=ttl,
            session_factory=factory,
        )
        if lease is None:
            return False

    def _load_meta():
        with factory() as db:
            run = db.get(AgentRun, run_id)
            if (
                not run
                or run.status not in ACTIVE
                or run.lease_owner != lease.owner
                or run.lease_token != lease.token
            ):
                return None
            session = db.get(ChatSession, run.session_id)
            user = (
                db.get(User, session.user_id)
                if session and session.tenant_id == run.tenant_id and session.deleted_at is None
                else None
            )
            personal = db.get(SupportCase, run.session_id)
            personal_error = None
            if personal is not None:
                try:
                    assert_editable(personal)
                except Exception as exc:
                    personal_error = exc
            return (
                run.tenant_id,
                run.session_id,
                run.status,
                run.data["question"],
                run.data["workflow"],
                user.id if user else None,
                user.username if user else None,
                user.tenant_id if user else None,
                personal_error,
            )

    meta = run_with_sqlite_retry(_load_meta)
    if meta is None:
        return False
    (
        run_tenant_id,
        run_session_id,
        run_status,
        run_question,
        run_workflow,
        user_id,
        user_username,
        user_tenant_id,
        personal_error,
    ) = meta

    recorder = RunRecorder(
        run_id,
        run_tenant_id,
        lease_owner=lease.owner,
        lease_token=lease.token,
        attempt=lease.attempt,
        session_factory=factory,
    )
    try:
        with _lease_heartbeat(
            lease,
            lease_seconds=ttl,
            heartbeat_interval=heartbeat_interval,
            on_lost=recorder.lease_lost.set,
            is_finished=lambda: recorder.finished,
            session_factory=factory,
        ):
            with capture_run(recorder), live_events():
                check_lease()
                if run_status == "cancel_requested" or not user_id or user_tenant_id != run_tenant_id:
                    raise RunCancelled()
                if personal_error is not None:
                    raise personal_error
                stream = agent_service._chat_stream(
                    container,
                    run_tenant_id,
                    user_id,
                    user_username,
                    run_session_id,
                    run_question,
                    run_id,
                    run_workflow,
                    background=True,
                )
                tokens = None
                flushed_at = time.monotonic()
                try:
                    for event in stream:
                        # 终态已由 _chat_stream 合法提交后，不再校验租约也不再写入已终态的 AgentRun。
                        if recorder.finished:
                            tokens = None
                            continue
                        check_lease()
                        check_cancelled()
                        if event["type"] == "token":
                            if tokens is None:
                                tokens = event
                            else:
                                tokens["delta"] += event["delta"]
                            if time.monotonic() - flushed_at < 0.25 and len(tokens["delta"]) < 1024:
                                continue
                        if tokens is not None:
                            recorder.add("stream", tokens)
                            _notify(run_id)
                            tokens = None
                            flushed_at = time.monotonic()
                        # 终态和完整回答直接从 AgentRun 读取。
                        if event["type"] not in ("token", "error", "final", "done"):
                            recorder.add("stream", event)
                            _notify(run_id)
                finally:
                    stream.close()
        return recorder.finished
    except LeaseLostError:
        if recorder.finished:
            return True
        logger.warning(
            "background_run_lease_lost run_id=%s owner=%s attempt=%s",
            run_id,
            lease.owner,
            lease.attempt,
        )
        return False
    except RunCancelled:
        if not recorder.finished:
            try:
                recorder.finish({"status": "cancelled", "end_reason": "user_cancelled"})
            except LeaseLostError:
                logger.warning("background_run_cancel_rejected_lease_lost run_id=%s owner=%s", run_id, lease.owner)
                return False
        return True
    except Exception as exc:
        logger.error("background_run_failed run_id=%s error_type=%s", run_id, type(exc).__name__)
        if not recorder.finished:
            try:
                recorder.finish({"status": "failed", "error_type": type(exc).__name__})
            except LeaseLostError:
                logger.warning("background_run_fail_rejected_lease_lost run_id=%s owner=%s", run_id, lease.owner)
                return False
        return True
    finally:
        _notify(run_id)


def read_events(run_id, tenant_id, user_id, after):
    factory = _resolve_session_factory()

    def _read():
        with factory() as db:
            # 先读 AgentRun 状态再按单调 seq 读事件：在 PG READ COMMITTED 下若已见到终态，后续事件快照必覆盖终态前全部提交。
            run = get_run(db, tenant_id, run_id, user_id)
            if run is None:
                return [], {"type": "error", "message": "运行记录不可访问", "run_id": run_id}
            events = (
                db.query(AgentRunEvent)
                .filter_by(run_id=run_id, tenant_id=tenant_id, kind="stream")
                .filter(AgentRunEvent.seq > after)
                .order_by(AgentRunEvent.seq)
                .limit(100)
                .all()
            )
            rows = [(row.seq, row.detail) for row in events]
            if len(rows) == 100 or run.status in ACTIVE:
                return rows, None
            result = run.data.get("result", {})
            if run.status == "cancelled":
                terminal = {"type": "cancelled", "message": "已停止，已有证据保留"}
            elif "answer" in result:
                terminal = {**result, "type": "final"}
            else:
                terminal = {"type": "error", "message": result.get("error") or "本次调查未完成，已有证据保留"}
            return rows, {**terminal, "run_id": run_id, "status": run.status}

    return run_with_sqlite_retry(_read)


async def subscribe(run_id, tenant_id, user_id, after=0):
    yield agent_service._sse({"type": "connected", "run_id": run_id})
    last_activity = time.monotonic()
    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    entry = (loop, wake)
    with _waiter_lock:
        _waiters.setdefault(run_id, set()).add(entry)
    try:
        while True:
            wake.clear()
            rows, terminal = await run_in_threadpool(read_events, run_id, tenant_id, user_id, after)
            for after, event in rows:
                yield f"id: {after}\n" + agent_service._sse(event)
            if terminal:
                yield agent_service._sse(terminal)
                yield agent_service._sse({"type": "done"})
                return
            if rows:
                last_activity = time.monotonic()
            elif time.monotonic() - last_activity > 10:
                yield ": keep-alive\n\n"
                last_activity = time.monotonic()
            if len(rows) < 100:
                try:
                    await asyncio.wait_for(wake.wait(), timeout=0.35)
                except asyncio.TimeoutError:
                    pass
    finally:
        with _waiter_lock:
            group = _waiters.get(run_id)
            if group is not None:
                group.discard(entry)
                if not group:
                    _waiters.pop(run_id, None)

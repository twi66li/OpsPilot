"""基于数据库事务租约的知识库入库队列：领取、心跳续约、过期重领与幂等激活。"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from importlib.metadata import version as package_version

from sqlalchemy import or_, select, update

from app.config import get_settings
from app.db import base as db_base
from app.db.base import SessionLocal, run_with_sqlite_retry
from app.db.models import Document, DocumentChunk, DocumentParent, DocumentVersion, User, _now
from app.services.assistant_quota import model_allowance
from app.services.budget import BudgetExceededError
from app.services.document_parser import build_version_chunks, parse_file
from app.services.run_service import (
    LeaseLostError,
    TaskLease,
    _dialect_name,
    make_lease_token,
    make_worker_id,
)

logger = logging.getLogger(__name__)
_INITIAL_SESSION_LOCAL = SessionLocal
_worker: ThreadPoolExecutor | None = None
_runtimes: dict[int, "_WorkerRuntime"] = {}
_runtime_lock = threading.Lock()
CLAIMABLE_STATUSES = ("uploaded", "queued", "parsing", "chunking", "indexing")


@dataclass
class _WorkerRuntime:
    executor: ThreadPoolExecutor
    worker_id: str
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


def _current_runtime() -> _WorkerRuntime | None:
    worker = _worker
    if worker is None:
        return None
    with _runtime_lock:
        return _runtimes.get(id(worker))


def make_version_idempotency_key(version_id: str, doc_id: str, input_hash: str | None = None) -> str:
    if input_hash:
        return f"ingest:{doc_id}:{input_hash[:48]}"
    return f"ingest:{version_id}"


def worker_ready():
    return _worker is not None


def claim_next_version(
    worker_id: str,
    *,
    lease_seconds: float | None = None,
    now: datetime | None = None,
    version_id: str | None = None,
    session_factory=None,
) -> TaskLease | None:
    """在数据库事务内领取一条待入库或租约已过期的文档版本任务。

    PostgreSQL 使用 SELECT ... FOR UPDATE SKIP LOCKED 避免多 worker 阻塞与冲突；
    SQLite 使用事务串行 + 条件 UPDATE（CAS）保证同一版本只被唯一 worker 领取。
    """
    factory = _resolve_session_factory(session_factory)
    ttl = float(lease_seconds if lease_seconds is not None else get_settings().task_lease_seconds)

    def _claim() -> TaskLease | None:
        ts = now or _now()
        expires_at = ts + timedelta(seconds=ttl)
        conditions = [
            DocumentVersion.status.in_(CLAIMABLE_STATUSES),
            or_(
                DocumentVersion.lease_owner.is_(None),
                DocumentVersion.lease_expires_at.is_(None),
                DocumentVersion.lease_expires_at <= ts,
            ),
        ]
        if version_id is not None:
            conditions.append(DocumentVersion.id == version_id)

        with factory() as db:
            dialect = _dialect_name(db)
            if dialect == "postgresql":
                stmt = (
                    select(DocumentVersion)
                    .where(*conditions)
                    .order_by(DocumentVersion.created_at.asc(), DocumentVersion.id.asc())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                row = db.scalars(stmt).first()
                if row is None:
                    return None
                next_attempt = (row.attempt or 0) + 1
                token = make_lease_token(next_attempt)
                if row.status == "uploaded":
                    row.status = "queued"
                if not row.idempotency_key:
                    candidate_key = make_version_idempotency_key(
                        row.id,
                        row.doc_id,
                        (row.index_config or {}).get("input_hash"),
                    )
                    conflict = (
                        db.query(DocumentVersion.id)
                        .filter_by(tenant_id=row.tenant_id, idempotency_key=candidate_key)
                        .first()
                    )
                    row.idempotency_key = candidate_key if conflict is None else f"ingest:{row.id}"
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
                select(
                    DocumentVersion.id,
                    DocumentVersion.attempt,
                    DocumentVersion.status,
                    DocumentVersion.tenant_id,
                    DocumentVersion.doc_id,
                    DocumentVersion.idempotency_key,
                    DocumentVersion.index_config,
                )
                .where(*conditions)
                .order_by(DocumentVersion.created_at.asc(), DocumentVersion.id.asc())
                .limit(1 if version_id is not None else 8)
            ).all()
            for cand_id, cand_attempt, cand_status, cand_tenant, cand_doc_id, cand_idem_key, cand_cfg in candidates:
                current_attempt = cand_attempt or 0
                next_attempt = current_attempt + 1
                token = make_lease_token(next_attempt)
                values = {
                    "attempt": next_attempt,
                    "lease_owner": worker_id,
                    "lease_token": token,
                    "heartbeat_at": ts,
                    "lease_expires_at": expires_at,
                    "updated_at": ts,
                }
                if cand_status == "uploaded":
                    values["status"] = "queued"
                if not cand_idem_key:
                    candidate_key = make_version_idempotency_key(
                        cand_id,
                        cand_doc_id,
                        (cand_cfg or {}).get("input_hash"),
                    )
                    conflict = (
                        db.query(DocumentVersion.id)
                        .filter_by(tenant_id=cand_tenant, idempotency_key=candidate_key)
                        .first()
                    )
                    values["idempotency_key"] = candidate_key if conflict is None else f"ingest:{cand_id}"
                updated = db.execute(
                    update(DocumentVersion)
                    .where(
                        DocumentVersion.id == cand_id,
                        DocumentVersion.status.in_(CLAIMABLE_STATUSES),
                        DocumentVersion.attempt == current_attempt,
                        or_(
                            DocumentVersion.lease_owner.is_(None),
                            DocumentVersion.lease_expires_at.is_(None),
                            DocumentVersion.lease_expires_at <= ts,
                        ),
                    )
                    .values(**values)
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


def claim_version_by_id(
    version_id: str,
    worker_id: str,
    *,
    lease_seconds: float | None = None,
    now: datetime | None = None,
    session_factory=None,
) -> TaskLease | None:
    return claim_next_version(
        worker_id,
        lease_seconds=lease_seconds,
        now=now,
        version_id=version_id,
        session_factory=session_factory,
    )


def renew_version_lease(
    version_id: str,
    lease_owner: str,
    lease_token: str,
    *,
    lease_seconds: float | None = None,
    now: datetime | None = None,
    session_factory=None,
) -> bool:
    """按 (lease_owner, lease_token) 条件续约入库任务心跳，失租返回 False。"""
    factory = _resolve_session_factory(session_factory)
    ttl = float(lease_seconds if lease_seconds is not None else get_settings().task_lease_seconds)

    def _renew() -> bool:
        ts = now or _now()
        expires_at = ts + timedelta(seconds=ttl)
        with factory() as db:
            updated = db.execute(
                update(DocumentVersion)
                .where(
                    DocumentVersion.id == version_id,
                    DocumentVersion.status.in_(CLAIMABLE_STATUSES),
                    DocumentVersion.lease_owner == lease_owner,
                    DocumentVersion.lease_token == lease_token,
                    DocumentVersion.lease_expires_at > ts,
                )
                .values(heartbeat_at=ts, lease_expires_at=expires_at, updated_at=ts)
            ).rowcount
            db.commit()
            return updated == 1

    return run_with_sqlite_retry(_renew)


def advance_version_stage(
    version_id: str,
    lease: TaskLease,
    *,
    lease_seconds: float | None = None,
    session_factory=None,
    **fields,
) -> bool:
    """携带 fencing token 推进版本状态/进度；旧领取者提交返回 False。"""
    factory = _resolve_session_factory(session_factory)

    def _advance() -> bool:
        now = _now()
        values = {
            **fields,
            "heartbeat_at": now,
            "updated_at": now,
        }
        if lease_seconds is not None:
            values["lease_expires_at"] = now + timedelta(seconds=float(lease_seconds))
        with factory() as db:
            updated = db.execute(
                update(DocumentVersion)
                .where(
                    DocumentVersion.id == version_id,
                    DocumentVersion.status.in_(CLAIMABLE_STATUSES),
                    DocumentVersion.lease_owner == lease.owner,
                    DocumentVersion.lease_token == lease.token,
                    DocumentVersion.lease_expires_at > now,
                )
                .values(**values)
            ).rowcount
            if updated != 1:
                db.rollback()
                return False
            db.commit()
            return True

    return run_with_sqlite_retry(_advance)


def _assert_lease_active(version_id: str, lease: TaskLease, lost_event: threading.Event, session_factory=None) -> None:
    if lost_event.is_set():
        raise LeaseLostError(f"DocumentVersion 租约已失效: version_id={version_id} owner={lease.owner}")
    factory = _resolve_session_factory(session_factory)

    def _check() -> bool:
        with factory() as db:
            row = db.execute(
                select(
                    DocumentVersion.status,
                    DocumentVersion.lease_owner,
                    DocumentVersion.lease_token,
                    DocumentVersion.lease_expires_at,
                ).where(DocumentVersion.id == version_id)
            ).first()
            if row is None:
                return False
            status, owner, token, expires_at = row
            return (status in CLAIMABLE_STATUSES and owner == lease.owner and token == lease.token
                    and expires_at is not None and expires_at > _now())

    if not run_with_sqlite_retry(_check):
        lost_event.set()
        raise LeaseLostError(f"DocumentVersion 租约已易主或终态: version_id={version_id} owner={lease.owner}")


def persist_version_chunks_with_lease(
    version_id: str,
    lease: TaskLease,
    *,
    doc_id: str,
    tenant_id: str,
    parents: list[dict],
    chunks: list[dict],
    index_config: dict,
    lease_seconds: float | None = None,
    session_factory=None,
) -> bool:
    """在单事务内先校验租约令牌并推进到 indexing，再幂等对齐父块与子块（无重复分块）。"""
    factory = _resolve_session_factory(session_factory)

    def _persist() -> bool:
        now = _now()
        values = {
            "status": "indexing",
            "progress": 45,
            "index_config": index_config,
            "heartbeat_at": now,
            "updated_at": now,
        }
        if lease_seconds is not None:
            values["lease_expires_at"] = now + timedelta(seconds=float(lease_seconds))
        with factory() as db:
            updated = db.execute(
                update(DocumentVersion)
                .where(
                    DocumentVersion.id == version_id,
                    DocumentVersion.status.in_(CLAIMABLE_STATUSES),
                    DocumentVersion.lease_owner == lease.owner,
                    DocumentVersion.lease_token == lease.token,
                    DocumentVersion.lease_expires_at > now,
                )
                .values(**values)
            ).rowcount
            if updated != 1:
                db.rollback()
                return False

            existing_parents = {
                row.id: row
                for row in db.query(DocumentParent).filter_by(version_id=version_id).all()
            }
            desired_parent_ids = {p["id"] for p in parents}
            for stale_id, stale_row in existing_parents.items():
                if stale_id not in desired_parent_ids:
                    db.delete(stale_row)
            for parent in parents:
                cur = existing_parents.get(parent["id"])
                if cur is None:
                    db.add(DocumentParent(tenant_id=tenant_id, doc_id=doc_id, version_id=version_id, **parent))
                else:
                    if cur.text != parent["text"] or cur.location != parent["location"]:
                        cur.text = parent["text"]
                        cur.location = parent["location"]

            existing_chunks = {
                row.id: row
                for row in db.query(DocumentChunk).filter_by(version_id=version_id).all()
            }
            desired_chunk_ids = {c["chunk_id"] for c in chunks}
            for stale_id, stale_row in existing_chunks.items():
                if stale_id not in desired_chunk_ids:
                    db.delete(stale_row)
            for chunk in chunks:
                cur = existing_chunks.get(chunk["chunk_id"])
                if cur is None:
                    db.add(
                        DocumentChunk(
                            id=chunk["chunk_id"],
                            tenant_id=tenant_id,
                            doc_id=doc_id,
                            version_id=version_id,
                            parent_id=chunk["parent_id"],
                            payload=chunk,
                        )
                    )
                else:
                    if cur.parent_id != chunk["parent_id"] or cur.payload != chunk:
                        cur.parent_id = chunk["parent_id"]
                        cur.payload = chunk

            db.commit()
            return True

    return run_with_sqlite_retry(_persist)


def complete_version_with_lease(
    version_id: str,
    lease: TaskLease,
    *,
    doc_content: str,
    chunk_count: int,
    session_factory=None,
) -> bool:
    """在单事务内校验租约令牌并原子完成版本 ready 落库与幂等激活；过期领取者返回 False。"""
    factory = _resolve_session_factory(session_factory)

    def _complete() -> bool:
        now = _now()
        with factory() as db:
            updated = db.execute(
                update(DocumentVersion)
                .where(
                    DocumentVersion.id == version_id,
                    DocumentVersion.status.in_(CLAIMABLE_STATUSES),
                    DocumentVersion.lease_owner == lease.owner,
                    DocumentVersion.lease_token == lease.token,
                    DocumentVersion.lease_expires_at > now,
                )
                .values(
                    status="ready",
                    progress=100,
                    error="",
                    lease_expires_at=None,
                    heartbeat_at=now,
                    updated_at=now,
                )
            ).rowcount
            if updated != 1:
                db.rollback()
                return False

            version = db.get(DocumentVersion, version_id)
            doc = db.get(Document, version.doc_id) if version is not None else None
            if version is not None:
                already_activated = bool(doc is not None and doc.active_version_id == version.id)
                prev_activations = int((version.index_config or {}).get("activation_count", 0))
                next_activations = prev_activations if already_activated and prev_activations > 0 else prev_activations + 1
                version.index_config = {
                    **(version.index_config or {}),
                    "committed_attempt": lease.attempt,
                    "committed_owner": lease.owner,
                    "committed_lease_token": lease.token,
                    "activation_count": next_activations,
                }
                if doc is not None and doc.deleted_at is None:
                    # 召回在两路搜索前都按此指针过滤；仅在版本合法完成且文档未删除时幂等激活。
                    if not already_activated:
                        doc.active_version_id = version.id
                    doc.content = doc_content
                    doc.chunk_count = chunk_count
            db.commit()
            return True

    return run_with_sqlite_retry(_complete)


def fail_version_with_lease(
    version_id: str,
    lease: TaskLease,
    *,
    failure_stage: str,
    failure_type: str,
    error_message: str,
    session_factory=None,
) -> bool:
    """仅当仍持有有效租约时将版本标记为 failed；避免过期 worker 破坏新 worker 状态。"""
    factory = _resolve_session_factory(session_factory)

    def _fail() -> bool:
        now = _now()
        with factory() as db:
            version = db.get(DocumentVersion, version_id)
            if version is None:
                return False
            new_cfg = {
                **(version.index_config or {}),
                "failure_stage": failure_stage,
                "failure_type": failure_type,
            }
            updated = db.execute(
                update(DocumentVersion)
                .where(
                    DocumentVersion.id == version_id,
                    DocumentVersion.status.in_(CLAIMABLE_STATUSES),
                    DocumentVersion.lease_owner == lease.owner,
                    DocumentVersion.lease_token == lease.token,
                    DocumentVersion.lease_expires_at > now,
                )
                .values(
                    status="failed",
                    error=error_message,
                    index_config=new_cfg,
                    lease_expires_at=None,
                    heartbeat_at=now,
                    updated_at=now,
                )
            ).rowcount
            if updated != 1:
                db.rollback()
                return False
            db.commit()
            return True

    return run_with_sqlite_retry(_fail)


@contextmanager
def _lease_heartbeat(
    lease: TaskLease,
    *,
    lease_seconds: float,
    heartbeat_interval: float,
    lost_event: threading.Event,
    session_factory=None,
):
    stop = threading.Event()
    interval = max(0.02, min(float(heartbeat_interval), max(0.02, float(lease_seconds) / 3.0)))

    def _loop():
        while not stop.wait(interval):
            try:
                ok = renew_version_lease(
                    lease.task_id,
                    lease.owner,
                    lease.token,
                    lease_seconds=lease_seconds,
                    session_factory=session_factory,
                )
                if not ok:
                    lost_event.set()
                    return
            except Exception as exc:
                logger.warning(
                    "ingestion_heartbeat_failed version_id=%s owner=%s error_type=%s",
                    lease.task_id,
                    lease.owner,
                    type(exc).__name__,
                )

    thread = threading.Thread(
        target=_loop,
        name=f"ingest-heartbeat-{lease.task_id[:8]}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2.0)


def _run_claimed(runtime: _WorkerRuntime, lease: TaskLease, container) -> None:
    try:
        process_version(
            lease.task_id,
            container,
            lease=lease,
            lease_seconds=getattr(container.settings, "task_lease_seconds", get_settings().task_lease_seconds),
        )
    finally:
        with runtime.slot_lock:
            runtime.inflight = max(0, runtime.inflight - 1)
        runtime.wake_event.set()


def _dispatch_available(runtime: _WorkerRuntime, container) -> int:
    dispatched = 0
    lease_seconds = getattr(container.settings, "task_lease_seconds", get_settings().task_lease_seconds)
    while not runtime.stop_event.is_set():
        with runtime.slot_lock:
            if runtime.inflight >= 1:
                break
            runtime.inflight += 1
        try:
            lease = claim_next_version(runtime.worker_id, lease_seconds=lease_seconds)
        except Exception as exc:
            with runtime.slot_lock:
                runtime.inflight = max(0, runtime.inflight - 1)
            logger.warning("ingestion_claim_failed worker=%s error_type=%s", runtime.worker_id, type(exc).__name__)
            break
        if lease is None:
            with runtime.slot_lock:
                runtime.inflight = max(0, runtime.inflight - 1)
            break
        try:
            runtime.executor.submit(_run_claimed, runtime, lease, container)
            dispatched += 1
        except RuntimeError:
            with runtime.slot_lock:
                runtime.inflight = max(0, runtime.inflight - 1)
            break
    return dispatched


def start_worker(*, worker_id: str | None = None):
    global _worker
    from app.container import get_container

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="knowledge-ingest")
    runtime = _WorkerRuntime(executor=executor, worker_id=worker_id or make_worker_id("ingest"))
    with _runtime_lock:
        _worker = executor
        _runtimes[id(executor)] = runtime

    # 启动后立即领取一轮待入库或租约已过期的版本任务（不再粗暴将进行中任务标为失败）。
    _dispatch_available(runtime, get_container())

    def _poll():
        while not runtime.stop_event.is_set():
            poll_interval = max(0.05, float(get_settings().task_poll_interval_seconds))
            runtime.wake_event.wait(timeout=poll_interval)
            runtime.wake_event.clear()
            if runtime.stop_event.is_set():
                break
            _dispatch_available(runtime, get_container())

    runtime.poll_thread = threading.Thread(
        target=_poll,
        name=f"ingest-poller-{runtime.worker_id[-8:]}",
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


def enqueue(version_id: str, container, *, idempotency_key: str | None = None, session_factory=None) -> str | None:
    """幂等将文档版本放入数据库入库队列；已 ready 或已有有效租约的版本不会重复重置。"""
    if _worker is None:
        raise RuntimeError("入库工作线程尚未启动")
    factory = _resolve_session_factory(session_factory)

    def _enqueue() -> tuple[str | None, bool]:
        now = _now()
        with factory() as db:
            version = db.get(DocumentVersion, version_id)
            if version is None:
                return None, False
            target_key = idempotency_key or version.idempotency_key or make_version_idempotency_key(
                version.id,
                version.doc_id,
                (version.index_config or {}).get("input_hash"),
            )
            existing = (
                db.query(DocumentVersion)
                .filter_by(tenant_id=version.tenant_id, idempotency_key=target_key)
                .first()
            )
            if existing is not None and existing.id != version.id:
                should_wake = existing.status in CLAIMABLE_STATUSES and (
                    existing.lease_owner is None
                    or existing.lease_expires_at is None
                    or existing.lease_expires_at <= now
                )
                return existing.id, should_wake
            if version.status == "ready":
                return version.id, False
            changed = False
            if version.idempotency_key != target_key:
                version.idempotency_key = target_key
                changed = True
            if version.status == "uploaded":
                version.status = "queued"
                version.updated_at = now
                if version.lease_expires_at is None or version.lease_expires_at <= now:
                    version.lease_owner = None
                    version.lease_token = None
                    version.lease_expires_at = None
                changed = True
            if changed:
                db.commit()
            should_wake = version.status in CLAIMABLE_STATUSES and (
                version.lease_owner is None
                or version.lease_expires_at is None
                or version.lease_expires_at <= now
            )
            return version.id, should_wake

    target_id, should_wake = run_with_sqlite_retry(_enqueue)
    if target_id is None:
        return None
    if should_wake:
        runtime = _current_runtime()
        if runtime is not None:
            runtime.wake_event.set()
            _dispatch_available(runtime, container)
        else:
            _worker.submit(process_version, target_id, container)
    return target_id


def run_once(
    container,
    *,
    worker_id: str | None = None,
    version_id: str | None = None,
    lease_seconds: float | None = None,
    now: datetime | None = None,
    session_factory=None,
) -> str | None:
    """单次领取并执行一条入库任务（供测试及独立 worker 驱动）。"""
    owner = worker_id or make_worker_id("ingest")
    lease = claim_next_version(
        owner,
        lease_seconds=lease_seconds,
        now=now,
        version_id=version_id,
        session_factory=session_factory,
    )
    if lease is None:
        return None
    ok = process_version(
        lease.task_id,
        container,
        lease=lease,
        lease_seconds=lease_seconds,
        session_factory=session_factory,
    )
    return lease.task_id if ok else None


def process_version(
    version_id: str,
    container,
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
        lease = claim_version_by_id(
            version_id,
            worker_id or make_worker_id("ingest"),
            lease_seconds=ttl,
            session_factory=factory,
        )
        if lease is None:
            return False

    lost_event = threading.Event()
    current_stage = "queued"
    try:
        with factory() as db:
            version = db.get(DocumentVersion, version_id)
            if (
                version is None
                or version.status not in CLAIMABLE_STATUSES
                or version.lease_owner != lease.owner
                or version.lease_token != lease.token
            ):
                return False
            doc = db.get(Document, version.doc_id)
            if doc is None:
                raise ValueError("文档记录不存在")
            doc_id = doc.id
            doc_tenant_id = doc.tenant_id
            doc_title = doc.title
            doc_source = doc.source
            owner = db.get(User, doc.created_by) if doc.created_by else None
            owner_quota = (owner.id, owner.tenant_id, owner.role) if owner else ("", doc_tenant_id, "admin")
            original_path = version.original_path
            filename = version.filename
            parsed_path = version.parsed_path
            version_number = version.number
            base_index_config = dict(version.index_config or {})

        with _lease_heartbeat(
            lease,
            lease_seconds=ttl,
            heartbeat_interval=heartbeat_interval,
            lost_event=lost_event,
            session_factory=factory,
        ):
            current_stage = "parsing"
            if not advance_version_stage(
                version_id,
                lease,
                status="parsing",
                progress=10,
                error="",
                lease_seconds=ttl,
                session_factory=factory,
            ):
                raise LeaseLostError(f"parsing 阶段租约失效: version_id={version_id}")

            pages, warnings, parser = parse_file(original_path, filename, parsed_path)
            _assert_lease_active(version_id, lease, lost_event, session_factory=factory)

            current_stage = "chunking"
            if not advance_version_stage(
                version_id,
                lease,
                pages=pages,
                warnings=warnings,
                parser=parser,
                status="chunking",
                progress=30,
                lease_seconds=ttl,
                session_factory=factory,
            ):
                raise LeaseLostError(f"chunking 阶段租约失效: version_id={version_id}")

            # 使用轻量只读快照调用确定性分块，避免长时间占用数据库会话。
            doc_stub = type("_DocView", (), {"id": doc_id, "tenant_id": doc_tenant_id, "title": doc_title, "source": doc_source})()
            ver_stub = type("_VerView", (), {"id": version_id, "number": version_number, "filename": filename, "parser": parser})()
            parents, chunks = build_version_chunks(doc_stub, ver_stub, pages)
            if not chunks:
                raise ValueError("未生成可检索片段，请检查正文")

            next_index_config = {
                **base_index_config,
                "embedding_model": container.settings.embedding_model,
                "embedding_dim": container.settings.embedding_dim,
                "chunking_version": "structure-v1",
                "chunk_count": len(chunks),
                "parser_version": package_version("pypdf") if parser == "pypdf" else "v1",
            }
            current_stage = "indexing"
            if not persist_version_chunks_with_lease(
                version_id,
                lease,
                doc_id=doc_id,
                tenant_id=doc_tenant_id,
                parents=parents,
                chunks=chunks,
                index_config=next_index_config,
                lease_seconds=ttl,
                session_factory=factory,
            ):
                raise LeaseLostError(f"分块持久化阶段租约失效: version_id={version_id}")

            for offset in range(0, len(chunks), 16):
                _assert_lease_active(version_id, lease, lost_event, session_factory=factory)
                batch = chunks[offset : offset + 16]
                with model_allowance(*owner_quota):
                    vectors = container.embedder.embed([c["text"] for c in batch])
                if len(vectors) != len(batch):
                    raise ValueError("Embedding返回数量与片段数量不一致")
                _assert_lease_active(version_id, lease, lost_event, session_factory=factory)
                container.vector_store.upsert(doc_tenant_id, batch, vectors)
                progress = 45 + int(50 * min(offset + 16, len(chunks)) / len(chunks))
                if not advance_version_stage(
                    version_id,
                    lease,
                    progress=progress,
                    lease_seconds=ttl,
                    session_factory=factory,
                ):
                    raise LeaseLostError(f"indexing 进度更新租约失效: version_id={version_id}")

            _assert_lease_active(version_id, lease, lost_event, session_factory=factory)
            container.bm25.add(doc_tenant_id, chunks)

            doc_content = "\n\n".join(p["text"] for p in pages)
            if not complete_version_with_lease(
                version_id,
                lease,
                doc_content=doc_content,
                chunk_count=len(chunks),
                session_factory=factory,
            ):
                raise LeaseLostError(f"版本完成激活阶段租约失效: version_id={version_id}")
        return True
    except LeaseLostError:
        logger.warning(
            "ingestion_lease_lost version_id=%s owner=%s attempt=%s stage=%s",
            version_id,
            lease.owner,
            lease.attempt,
            current_stage,
        )
        return False
    except Exception as exc:
        error_msg = f"{current_stage}阶段失败：{type(exc).__name__}，请检查文档或服务后重试"
        if isinstance(exc, (ValueError, BudgetExceededError)):
            error_msg += f"；{str(exc)[:300]}"
        fail_version_with_lease(
            version_id,
            lease,
            failure_stage=current_stage,
            failure_type=type(exc).__name__,
            error_message=error_msg,
            session_factory=factory,
        )
        logger.exception("知识版本 %s 入库失败", version_id)
        return False

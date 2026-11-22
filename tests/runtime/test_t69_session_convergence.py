"""T69 验收测试：同步 Session 收敛、PG 连接池配额核算、数据库原子事件序号与 SSE 断线重连。"""

import asyncio
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.config import Settings, calculate_db_connection_budget
from app.container import get_container
from app.db.base import SessionLocal, _make_engine
from app.db.models import AgentRun, AgentRunEvent
from app.services import background_runs, run_service
from app.services.run_service import RunRecorder, request_cancel, serialize_run
from tests.conftest import new_session


def _parse_sse_with_ids(raw_text: str) -> list[tuple[int | None, dict]]:
    items: list[tuple[int | None, dict]] = []
    current_id: int | None = None
    for line in raw_text.splitlines():
        if line.startswith("id: "):
            current_id = int(line.removeprefix("id: ").strip())
        elif line.startswith("data: "):
            items.append((current_id, json.loads(line.removeprefix("data: "))))
            current_id = None
    return items


def test_async_session_and_write_lock_removed():
    root = Path(__file__).resolve().parents[2]
    assert not (root / "app" / "db" / "async_session.py").exists()
    assert not hasattr(run_service, "_write_lock")

    forbidden = ("get_async_db", "AsyncSession", "async_session", "close_async_db", "_write_lock", "asyncpg", "aiosqlite")
    for py_file in (root / "app").rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in content, f"{py_file} 仍包含残留引用: {token}"

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "asyncpg" not in pyproject
    assert "aiosqlite" not in pyproject
    assert "SQLAlchemy[asyncio]" not in pyproject


def test_pg_connection_budget_fits_two_replicas_within_30():
    settings = Settings(database_url="postgresql+psycopg://u:p@127.0.0.1:5432/opspilot")
    budget = calculate_db_connection_budget(settings)

    assert budget.pg_max_connections == 30
    assert budget.replicas == 2
    assert budget.app_pool_size == 5
    assert budget.app_max_overflow == 3
    assert budget.app_pool_max == 8
    assert budget.checkpoint_pool_min == 1
    assert budget.checkpoint_pool_max == 4
    assert budget.per_replica_max == 12
    assert budget.total_app_max == 24
    assert budget.admin_reserved == 6
    assert budget.total_budget == 30
    assert budget.headroom >= 6
    assert budget.fits is True

    pg_engine = _make_engine(settings.database_url, settings=settings)
    try:
        assert pg_engine.pool.size() == budget.app_pool_size
        assert pg_engine.pool._max_overflow == budget.app_max_overflow
    finally:
        pg_engine.dispose()


def test_pg_connection_budget_rejects_oversubscription():
    with pytest.raises(ValueError, match="连接池总配额超限"):
        Settings(
            database_url="postgresql+psycopg://u:p@127.0.0.1:5432/opspilot",
            db_max_connections=30,
            db_replica_budget=2,
            db_admin_reserved_connections=6,
            db_pool_size=8,
            db_max_overflow=4,
            checkpoint_pool_max_size=4,
        )

    with pytest.raises(ValueError, match="CHECKPOINT_POOL_MIN_SIZE"):
        Settings(checkpoint_pool_min_size=4, checkpoint_pool_max_size=2)


def test_concurrent_event_appends_have_strict_monotonic_seq_without_gaps(client, admin_headers):
    session_id = new_session(client, admin_headers, "并发事件序号验证")
    container = get_container()
    recorder = RunRecorder.start(container, "default", session_id, "并发写入测试", request_id=f"conc-{uuid.uuid4().hex[:8]}")

    worker_count = 8
    events_per_worker = 10
    barrier = threading.Barrier(worker_count)

    def _writer(worker_id: int) -> list[int]:
        barrier.wait()
        seqs = []
        for idx in range(events_per_worker):
            seq = recorder.add("progress", {"worker": worker_id, "idx": idx})
            seqs.append(seq)
        return seqs

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(_writer, w) for w in range(worker_count)]
        returned_seqs = [seq for fut in futures for seq in fut.result()]

    recorder.finish({"status": "completed", "end_reason": "answered", "answer": "ok"})

    expected_total = 1 + worker_count * events_per_worker + 1  # started + 80 progress + finished
    assert sorted(returned_seqs) == list(range(2, 2 + worker_count * events_per_worker))

    with SessionLocal() as db:
        run = db.get(AgentRun, recorder.id)
        assert run is not None
        assert run.status == "completed"
        assert run.event_seq == expected_total
        events = (
            db.query(AgentRunEvent)
            .filter_by(run_id=recorder.id, tenant_id="default")
            .order_by(AgentRunEvent.seq)
            .all()
        )
        assert [e.seq for e in events] == list(range(1, expected_total + 1))
        assert events[0].kind == "started"
        assert events[-1].kind == "finished"
        payloads = {(e.detail["worker"], e.detail["idx"]) for e in events if e.kind == "progress"}
        assert len(payloads) == worker_count * events_per_worker
        serialized = serialize_run(db, run)
        assert [e["seq"] for e in serialized["data"]["events"]] == list(range(1, expected_total + 1))


def test_multi_session_concurrent_start_and_atomic_status_transitions(client, admin_headers, monkeypatch):
    container = get_container()
    monkeypatch.setattr(container.settings, "max_concurrent_runs", 2)
    sessions = [new_session(client, admin_headers, f"多会话并发-{i}") for i in range(4)]
    barrier = threading.Barrier(len(sessions))

    def _try_start(sid: str):
        barrier.wait()
        try:
            rec = RunRecorder.start(container, "default", sid, f"问题-{sid}", request_id=f"ms-{uuid.uuid4().hex[:8]}")
            return ("ok", rec)
        except HTTPException as exc:
            return ("err", exc.status_code)

    with ThreadPoolExecutor(max_workers=len(sessions)) as pool:
        outcomes = list(pool.map(_try_start, sessions))

    started = [item[1] for item in outcomes if item[0] == "ok"]
    rejected = [item[1] for item in outcomes if item[0] == "err"]
    assert len(started) == 2
    assert rejected == [429, 429]

    try:
        # 两个活跃运行同时并发写事件，各自 seq 独立严格递增
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(lambda: [started[0].add("progress", {"step": i}) for i in range(5)])
            f2 = pool.submit(lambda: [started[1].add("progress", {"step": i}) for i in range(5)])
            assert f1.result() == [2, 3, 4, 5, 6]
            assert f2.result() == [2, 3, 4, 5, 6]

        # 模拟跨进程取消：直接通过数据库原子状态迁移为 cancel_requested，不触发本进程 cancel_signal
        with SessionLocal() as db:
            run_row = db.get(AgentRun, started[1].id)
            run_row.status = "cancel_requested"
            db.commit()
        started[1].finish({"status": "completed", "end_reason": "answered", "answer": "should be cancelled"})

        started[0].finish({"status": "completed", "end_reason": "answered", "answer": "done"})
        # 已完成的运行不会被 request_cancel 回写为 cancel_requested
        with SessionLocal() as db:
            done_run = db.get(AgentRun, started[0].id)
            assert request_cancel(db, done_run) == "completed"
            cancelled_run = db.get(AgentRun, started[1].id)
            assert cancelled_run.status == "cancelled"
    finally:
        for rec in started:
            if not rec.finished:
                rec.finish({"status": "cancelled", "end_reason": "test_cleanup"})


def test_sse_cursor_reconnect_delivers_all_events_without_loss_or_duplicates(client, admin_headers):
    session_id = new_session(client, admin_headers, "SSE 断线游标重连")
    container = get_container()
    with SessionLocal() as db:
        from app.db.models import ChatSession

        user_id = db.get(ChatSession, session_id).user_id

    recorder = RunRecorder.start(
        container,
        "default",
        session_id,
        "模拟断线重连",
        request_id=f"sse-reconn-{uuid.uuid4().hex[:8]}",
        background=True,
    )

    try:
        # 第一批事件（穿插非 stream 事件验证游标按全局 seq 推进不丢失 stream 事件）
        for i in range(5):
            recorder.add("progress", {"note": f"p-{i}"})
            recorder.add("stream", {"type": "token", "delta": f"chunk-{i}", "index": i})

        async def _consume_first_batch(target_tokens: int):
            collected = []
            gen = background_runs.subscribe(recorder.id, "default", user_id, after=0)
            try:
                async for chunk in gen:
                    for cursor, payload in _parse_sse_with_ids(chunk):
                        if payload.get("type") == "token":
                            collected.append((cursor, payload))
                            if len(collected) >= target_tokens:
                                return collected
            finally:
                await gen.aclose()
            return collected

        first_part = asyncio.run(_consume_first_batch(5))
        assert [p["index"] for _, p in first_part] == [0, 1, 2, 3, 4]
        last_cursor = first_part[-1][0]
        assert isinstance(last_cursor, int) and last_cursor > 0

        # 模拟断线期间后台持续写入剩余事件并落盘终态
        for i in range(5, 12):
            recorder.add("stream", {"type": "token", "delta": f"chunk-{i}", "index": i})
            background_runs._notify(recorder.id)
        recorder.finish({"status": "answered", "end_reason": "answered", "answer": "完整回答"})
        background_runs._notify(recorder.id)

        # 客户端携带断线前游标重连 HTTP SSE 端点
        resp = client.get(f"/api/agent/runs/{recorder.id}/events?after={last_cursor}", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        second_items = _parse_sse_with_ids(resp.text)
        second_tokens = [(c, p) for c, p in second_items if p.get("type") == "token"]
        types = [p["type"] for _, p in second_items]

        assert types[0] == "connected"
        assert types[-2:] == ["final", "done"]
        final_event = next(p for _, p in second_items if p.get("type") == "final")
        assert final_event["answer"] == "完整回答"

        all_tokens = first_part + second_tokens
        assert [p["index"] for _, p in all_tokens] == list(range(12))
        cursors = [c for c, _ in all_tokens]
        assert cursors == sorted(set(cursors))
    finally:
        if not recorder.finished:
            recorder.finish({"status": "cancelled", "end_reason": "test_cleanup"})


def test_migration_0005_backfills_event_seq_and_enforces_uniqueness(tmp_path):
    db_url = "sqlite:///" + str(tmp_path / "upgrade_0005.db")
    eng = create_engine(db_url)
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        with eng.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "0004")
            conn.execute(text("INSERT INTO tenants (id, name, created_at) VALUES ('t1', 'T1', CURRENT_TIMESTAMP)"))
            conn.execute(
                text(
                    "INSERT INTO agent_runs (id, tenant_id, session_id, mode, status, data, created_at, updated_at) "
                    "VALUES ('run-legacy', 't1', 's1', 'rule', 'completed', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            for kind in ("started", "progress", "finished"):
                conn.execute(
                    text(
                        "INSERT INTO agent_run_events (run_id, tenant_id, kind, detail, created_at) "
                        "VALUES ('run-legacy', 't1', :kind, '{}', CURRENT_TIMESTAMP)"
                    ),
                    {"kind": kind},
                )
            command.upgrade(cfg, "head")
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
            assert conn.scalar(text("SELECT event_seq FROM agent_runs WHERE id='run-legacy'")) == 3
            seqs = conn.execute(
                text("SELECT seq, kind FROM agent_run_events WHERE run_id='run-legacy' ORDER BY id")
            ).fetchall()
            assert seqs == [(1, "started"), (2, "progress"), (3, "finished")]

        with pytest.raises(IntegrityError):
            with eng.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO agent_run_events (run_id, tenant_id, seq, kind, detail, created_at) "
                        "VALUES ('run-legacy', 't1', 2, 'dup', '{}', CURRENT_TIMESTAMP)"
                    )
                )
    finally:
        eng.dispose()

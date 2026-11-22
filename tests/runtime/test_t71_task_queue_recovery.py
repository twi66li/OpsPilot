"""T71 验收测试：后台调查与入库任务的数据库租约领取、心跳续约、故障恢复与幂等保护。"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.agent.state import DiagState
from app.auth.deps import AuthUser
from app.config import Settings
from app.container import get_container
from app.db.base import _make_engine, run_with_sqlite_retry
from app.db.models import (
    AgentRun,
    AgentRunEvent,
    ApprovalRequest,
    ChatSession,
    Document,
    DocumentChunk,
    DocumentParent,
    DocumentVersion,
    Message,
    Tenant,
    Ticket,
    User,
    _now,
)
from app.rag.bm25 import BM25Retriever
from app.rag.embeddings import MockEmbedding
from app.rag.vector_store import InMemoryVectorStore
from app.services import agent_service, background_runs, ingestion_service
from app.services.run_service import (
    LeaseLostError,
    RunRecorder,
    capture_run,
    make_message_idempotency_key,
)
from app.services.ticket_service import create_ticket, get_or_create_approval
from tests.conftest import new_session

ALEMBIC_INI = str(Path(__file__).resolve().parents[2] / "alembic.ini")


def _make_isolated_db(db_url: str):
    """创建并迁移独立数据库（支持 SQLite 文件库与真实 PostgreSQL）。"""
    eng = _make_engine(db_url)
    cfg = Config(ALEMBIC_INI)
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    return eng, factory


def _seed_tenant_user_session(factory, *, tenant_id="t-t71", user_id="u-t71", username="ops_t71"):
    session_id = uuid.uuid4().hex
    with factory() as db:
        if db.get(Tenant, tenant_id) is None:
            db.add(Tenant(id=tenant_id, name="T71 租户"))
        if db.get(User, user_id) is None:
            db.add(User(id=user_id, tenant_id=tenant_id, username=username, password_hash="x", role="admin"))
        db.add(ChatSession(id=session_id, tenant_id=tenant_id, user_id=user_id, title="T71 会话"))
        db.commit()
    return tenant_id, user_id, username, session_id


def _make_isolated_container(factory, *, graph=None, embedder=None, lease_seconds=5.0, heartbeat_interval=1.0):
    settings = Settings(
        _env_file=None,
        embedding_provider="mock",
        embedding_dim=32,
        llm_provider="mock",
        vector_store_backend="memory",
        max_concurrent_runs=4,
        task_lease_seconds=lease_seconds,
        task_heartbeat_interval_seconds=heartbeat_interval,
        task_poll_interval_seconds=0.05,
    )
    return SimpleNamespace(
        settings=settings,
        db_session=factory,
        graph=graph,
        embedder=embedder or MockEmbedding(dim=32),
        vector_store=InMemoryVectorStore(dim=32),
        bm25=BM25Retriever(),
        llm=SimpleNamespace(),
        cache=SimpleNamespace(),
    )


def _create_doc_and_version(factory, tmp_path: Path, *, tenant_id="t-t71", text_body: str, filename="runbook.md", idempotency_key=None):
    doc_id = uuid.uuid4().hex
    version_id = uuid.uuid4().hex
    file_path = tmp_path / f"{version_id}_{filename}"
    file_path.write_text(text_body, encoding="utf-8")
    with factory() as db:
        if db.get(Tenant, tenant_id) is None:
            db.add(Tenant(id=tenant_id, name="T71 租户"))
        db.add(Document(id=doc_id, tenant_id=tenant_id, title="故障手册", content="", source=filename))
        db.add(
            DocumentVersion(
                id=version_id,
                tenant_id=tenant_id,
                doc_id=doc_id,
                number=1,
                filename=filename,
                content_hash=uuid.uuid4().hex,
                original_path=str(file_path),
                parsed_path="",
                status="queued",
                index_config={"input_hash": uuid.uuid4().hex},
                idempotency_key=idempotency_key,
            )
        )
        db.commit()
    return doc_id, version_id


def test_migration_0007_upgrade_backfill_constraints_and_downgrade(tmp_path):
    """验证 0004->0005->0006->0007 完整迁移链升级、旧数据回填、唯一约束生效及 0007->0006 降级可逆。"""
    db_url = "sqlite:///" + str(tmp_path / "upgrade_0007.db")
    eng = create_engine(db_url)
    cfg = Config(ALEMBIC_INI)
    try:
        with eng.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "0004")
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
            command.upgrade(cfg, "0005")
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
            command.upgrade(cfg, "0006")
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
            conn.execute(text("INSERT INTO tenants (id, name, created_at) VALUES ('t1', 'T1', CURRENT_TIMESTAMP)"))
            conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, username, password_hash, role, created_at) "
                    "VALUES ('u1', 't1', 'u1', 'h', 'admin', CURRENT_TIMESTAMP)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO chat_sessions (id, tenant_id, user_id, title, created_at) "
                    "VALUES ('s1', 't1', 'u1', 'S1', CURRENT_TIMESTAMP)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO agent_runs (id, tenant_id, session_id, mode, status, event_seq, request_id, data, created_at, updated_at) "
                    "VALUES ('run-1', 't1', 's1', 'rule', 'completed', 1, 'req-1', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, title, source, content, chunk_count, created_by, created_at) "
                    "VALUES ('d1', 't1', 'D1', 'a.md', '', 0, 'u1', CURRENT_TIMESTAMP)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO document_versions "
                    "(id, tenant_id, doc_id, number, filename, content_hash, original_path, parsed_path, parser, status, progress, error, pages, warnings, index_config, created_at) "
                    "VALUES ('v1', 't1', 'd1', 1, 'a.md', 'h1', '/tmp/a.md', '', '', 'ready', 100, '', '[]', '[]', '{}', CURRENT_TIMESTAMP)"
                )
            )

            command.upgrade(cfg, "head")
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
            assert conn.scalar(text("SELECT idempotency_key FROM agent_runs WHERE id='run-1'")) == "run:s1:req-1"
            assert conn.scalar(text("SELECT attempt FROM agent_runs WHERE id='run-1'")) == 0
            assert conn.scalar(text("SELECT updated_at FROM document_versions WHERE id='v1'")) is not None

        # 验证 messages / agent_runs / document_versions / approval_requests 幂等唯一约束生效
        with pytest.raises(IntegrityError):
            with eng.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO messages (session_id, tenant_id, role, content, meta, idempotency_key, created_at) "
                        "VALUES ('s1', 't1', 'assistant', 'a', '{}', 'msg:run-1:assistant', CURRENT_TIMESTAMP)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO messages (session_id, tenant_id, role, content, meta, idempotency_key, created_at) "
                        "VALUES ('s1', 't1', 'assistant', 'b', '{}', 'msg:run-1:assistant', CURRENT_TIMESTAMP)"
                    )
                )

        with pytest.raises(IntegrityError):
            with eng.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO approval_requests (id, tenant_id, session_id, ticket_draft, status, requested_by, decided_by, comment, created_at) "
                        "VALUES ('ap1', 't1', 's1', '{}', 'pending', 'u1', '', '', CURRENT_TIMESTAMP)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO approval_requests (id, tenant_id, session_id, ticket_draft, status, requested_by, decided_by, comment, created_at) "
                        "VALUES ('ap2', 't1', 's1', '{}', 'pending', 'u1', '', '', CURRENT_TIMESTAMP)"
                    )
                )

        with eng.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "0006")
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
            command.upgrade(cfg, "head")
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
    finally:
        eng.dispose()


def test_investigation_scene_a_crash_after_claim_reclaim_and_stale_worker_rejected(tmp_path):
    """调查任务场景 A：worker A 领取后崩溃停止心跳 -> 过期重领 -> worker B 完成 -> 迟到的 A 提交被拒绝且结果唯一。"""
    db_url = "sqlite:///" + str(tmp_path / "inv_scene_a.db")
    eng, factory = _make_isolated_db(db_url)
    ckpt_conn = sqlite3.connect(tmp_path / "ckpt_a.sqlite", check_same_thread=False)
    try:
        tenant_id, user_id, username, session_id = _seed_tenant_user_session(factory)

        def answer_node(state):
            return {"answer": f"worker-b 确诊结论：{state['question']}", "end_reason": "answered"}

        sg = StateGraph(DiagState)
        sg.add_node("answer", answer_node)
        sg.add_edge(START, "answer")
        sg.add_edge("answer", END)
        compiled = sg.compile(checkpointer=SqliteSaver(ckpt_conn))
        container = _make_isolated_container(factory, graph=compiled, lease_seconds=2.0)

        queued_rec = RunRecorder.start(
            container,
            tenant_id,
            session_id,
            "排查数据库连接池耗尽 ERR_5003",
            request_id="req-scene-a-1",
            background=True,
            queued=True,
            session_factory=factory,
        )
        run_id = queued_rec.id

        # 1. Worker A 领取任务
        t0 = _now()
        lease_a = background_runs.claim_run_by_id(run_id, "worker-a", lease_seconds=2.0, now=t0, session_factory=factory)
        assert lease_a is not None
        assert lease_a.owner == "worker-a"
        assert lease_a.attempt == 1

        # 2. 租约未过期时 Worker B 无法抢占
        assert (
            background_runs.claim_run_by_id(
                run_id, "worker-b", lease_seconds=2.0, now=t0 + timedelta(seconds=1), session_factory=factory
            )
            is None
        )

        # 3. Worker A 崩溃停止心跳，租约过期后 Worker B 重领并递增 attempt / 生成新 lease_token
        t_expired = lease_a.expires_at + timedelta(seconds=1)
        lease_b = background_runs.claim_run_by_id(
            run_id, "worker-b", lease_seconds=10.0, now=t_expired, session_factory=factory
        )
        assert lease_b is not None
        assert lease_b.owner == "worker-b"
        assert lease_b.attempt == 2
        assert lease_b.token != lease_a.token

        # 4. Worker B 正常执行并落盘终态
        ok_b = background_runs.execute(container, run_id, lease=lease_b, lease_seconds=10.0, session_factory=factory)
        assert ok_b is True

        # 5. 迟到的 Worker A 复活：心跳续约、追加事件、提交终态、重新执行全部被明确拒绝
        assert background_runs.renew_run_lease(run_id, lease_a.owner, lease_a.token, session_factory=factory) is False
        stale_recorder = RunRecorder(
            run_id,
            tenant_id,
            lease_owner=lease_a.owner,
            lease_token=lease_a.token,
            attempt=lease_a.attempt,
            session_factory=factory,
        )
        with pytest.raises(LeaseLostError):
            stale_recorder.add("progress", {"note": "stale_event"})
        assert (
            background_runs.finish_run_with_lease(
                run_id,
                tenant_id,
                lease_a,
                {"status": "failed", "answer": "stale_overwrite", "end_reason": "stale"},
                session_factory=factory,
            )
            is False
        )
        assert background_runs.execute(container, run_id, lease=lease_a, session_factory=factory) is False

        # 6. 校验最终状态、租约审计字段与会话消息严格唯一
        with factory() as db:
            run = db.get(AgentRun, run_id)
            assert run.status == "answered"
            assert run.lease_owner == "worker-b"
            assert run.lease_token == lease_b.token
            assert run.attempt == 2
            assert run.lease_expires_at is None
            assert run.data["result"]["answer"] == "worker-b 确诊结论：排查数据库连接池耗尽 ERR_5003"
            msgs = db.query(Message).filter_by(session_id=session_id, tenant_id=tenant_id).order_by(Message.id).all()
            assert [m.role for m in msgs] == ["user", "assistant"]
            assert msgs[0].idempotency_key == make_message_idempotency_key(run_id, "user")
            assert msgs[1].idempotency_key == make_message_idempotency_key(run_id, "assistant")
            stale_events = (
                db.query(AgentRunEvent).filter_by(run_id=run_id, kind="progress").all()
            )
            assert all(e.detail.get("note") != "stale_event" for e in stale_events)
    finally:
        ckpt_conn.close()
        eng.dispose()


def test_investigation_scene_b_side_effects_committed_before_status_crash(tmp_path):
    """调查任务场景 B：助手消息与审批单/检查点已持久化但终态未提交即崩溃 -> 重领后幂等收口无重复副作用。"""
    db_url = "sqlite:///" + str(tmp_path / "inv_scene_b.db")
    eng, factory = _make_isolated_db(db_url)
    ckpt_conn = sqlite3.connect(tmp_path / "ckpt_b.sqlite", check_same_thread=False)
    try:
        tenant_id, user_id, username, session_id = _seed_tenant_user_session(factory)
        node_calls = []

        def diagnose_and_prepare(state):
            node_calls.append(state["run_id"])
            with factory() as db:
                req = get_or_create_approval(
                    db,
                    state["tenant_id"],
                    state["session_id"],
                    {"title": "连接池扩容工单", "description": "调整 maxPoolSize", "severity": "P1"},
                    requested_by=username,
                )
                ticket = create_ticket(
                    db,
                    state["tenant_id"],
                    state["session_id"],
                    req.ticket_draft,
                    created_by=username,
                    approver="admin",
                    approval_id=req.id,
                    comment="auto",
                )
                ticket_id = ticket.id
            return {
                "answer": "已完成根因定位并关联唯一工单",
                "ticket_id": ticket_id,
                "end_reason": "answered",
            }

        sg = StateGraph(DiagState)
        sg.add_node("diagnose", diagnose_and_prepare)
        sg.add_edge(START, "diagnose")
        sg.add_edge("diagnose", END)
        compiled = sg.compile(checkpointer=SqliteSaver(ckpt_conn))
        container = _make_isolated_container(factory, graph=compiled, lease_seconds=2.0)

        run_id = RunRecorder.start(
            container,
            tenant_id,
            session_id,
            "帮我排查 ERR_5003 并建单",
            request_id="req-scene-b-1",
            background=True,
            queued=True,
            session_factory=factory,
        ).id

        # Worker A 领取并完成外部步骤（输入消息、图检查点、审批单/工单、最终助手消息全部落库），
        # 但故意在 AgentRun.finish() 提交终态前模拟进程崩溃！
        t0 = _now()
        lease_a = background_runs.claim_run_by_id(run_id, "worker-a", lease_seconds=2.0, now=t0, session_factory=factory)
        assert lease_a is not None
        rec_a = RunRecorder(
            run_id,
            tenant_id,
            lease_owner=lease_a.owner,
            lease_token=lease_a.token,
            attempt=lease_a.attempt,
            session_factory=factory,
        )
        with capture_run(rec_a):
            with factory() as db:
                run_row = db.get(AgentRun, run_id)
                prepared = agent_service._background_input(
                    db,
                    run_row,
                    tenant_id,
                    session_id,
                    {
                        "question": run_row.data["question"],
                        "session_id": session_id,
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "username": username,
                        "run_id": run_id,
                        "evaluation_time": None,
                        "support_context": None,
                        "support_report": None,
                        "support_workflow": "auto",
                    },
                )
            config = {"configurable": {"thread_id": session_id}}
            compiled.invoke(prepared, config, durability="sync")
            snapshot = agent_service._final_snapshot(container, config)
            # 模拟最终助手消息已落库，但 AgentRun 仍停留在 running（未提交终态即崩溃）
            with factory() as db:
                agent_service._ensure_idempotent_message(
                    db,
                    tenant_id,
                    session_id,
                    "assistant",
                    snapshot["answer"],
                    meta={"run_id": run_id, "ticket_id": snapshot["ticket_id"]},
                    run_id=run_id,
                )
                db.commit()

        with factory() as db:
            assert db.get(AgentRun, run_id).status == "running"
            assert db.query(Message).filter_by(session_id=session_id).count() == 2
            assert db.query(ApprovalRequest).filter_by(session_id=session_id).count() == 1
            assert db.query(Ticket).filter_by(session_id=session_id).count() == 1

        # 租约过期后 Worker B 重领并完成终态提交
        lease_b = background_runs.claim_run_by_id(
            run_id,
            "worker-b",
            lease_seconds=10.0,
            now=lease_a.expires_at + timedelta(seconds=1),
            session_factory=factory,
        )
        assert lease_b is not None and lease_b.attempt == 2
        assert background_runs.execute(container, run_id, lease=lease_b, session_factory=factory) is True

        # 验证严格无重复消息、无重复审批单、无重复工单，且终态已安全落库
        with factory() as db:
            run = db.get(AgentRun, run_id)
            assert run.status == "answered"
            assert run.lease_owner == "worker-b"
            assert run.attempt == 2
            msgs = db.query(Message).filter_by(session_id=session_id, tenant_id=tenant_id).order_by(Message.id).all()
            assert [m.role for m in msgs] == ["user", "assistant"]
            assert db.query(ApprovalRequest).filter_by(session_id=session_id, tenant_id=tenant_id).count() == 1
            assert db.query(Ticket).filter_by(session_id=session_id, tenant_id=tenant_id).count() == 1
        assert len(node_calls) == 1  # 检查点恢复未重复执行副作用节点
    finally:
        ckpt_conn.close()
        eng.dispose()


def test_ingestion_scene_a_crash_after_claim_reclaim_and_stale_worker_rejected(tmp_path):
    """入库任务场景 A：worker A 领取推进到 parsing 后崩溃 -> 租约过期 worker B 重领完成 -> 迟到的 A 提交与激活全被拒绝。"""
    db_url = "sqlite:///" + str(tmp_path / "ingest_scene_a.db")
    eng, factory = _make_isolated_db(db_url)
    try:
        container = _make_isolated_container(factory, lease_seconds=2.0)
        doc_id, version_id = _create_doc_and_version(
            factory,
            tmp_path,
            text_body="# 数据库排障\n\nERR_T71_SCENE_A 发生时检查连接池与慢查询日志。",
        )

        # 1. Worker A 领取任务并推进到 parsing 后崩溃
        t0 = _now()
        lease_a = ingestion_service.claim_version_by_id(
            version_id, "ingest-a", lease_seconds=2.0, now=t0, session_factory=factory
        )
        assert lease_a is not None and lease_a.attempt == 1
        assert (
            ingestion_service.advance_version_stage(
                version_id, lease_a, status="parsing", progress=10, session_factory=factory
            )
            is True
        )

        # 2. 租约有效期内 Worker B 不可抢占
        assert (
            ingestion_service.claim_version_by_id(
                version_id, "ingest-b", lease_seconds=2.0, now=t0 + timedelta(seconds=1), session_factory=factory
            )
            is None
        )

        # 3. 租约过期后 Worker B 重领并完成入库与版本激活
        lease_b = ingestion_service.claim_version_by_id(
            version_id,
            "ingest-b",
            lease_seconds=10.0,
            now=lease_a.expires_at + timedelta(seconds=1),
            session_factory=factory,
        )
        assert lease_b is not None
        assert lease_b.owner == "ingest-b"
        assert lease_b.attempt == 2
        assert lease_b.token != lease_a.token
        assert ingestion_service.process_version(version_id, container, lease=lease_b, session_factory=factory) is True

        # 4. 迟到的 Worker A 复活：续约、状态推进、分块落库、标失败、版本激活全部被明确拒绝
        assert (
            ingestion_service.renew_version_lease(version_id, lease_a.owner, lease_a.token, session_factory=factory)
            is False
        )
        assert (
            ingestion_service.advance_version_stage(
                version_id, lease_a, status="indexing", progress=50, session_factory=factory
            )
            is False
        )
        assert (
            ingestion_service.persist_version_chunks_with_lease(
                version_id,
                lease_a,
                doc_id=doc_id,
                tenant_id="t-t71",
                parents=[],
                chunks=[],
                index_config={"stale": True},
                session_factory=factory,
            )
            is False
        )
        assert (
            ingestion_service.fail_version_with_lease(
                version_id,
                lease_a,
                failure_stage="parsing",
                failure_type="RuntimeError",
                error_message="stale error",
                session_factory=factory,
            )
            is False
        )
        assert (
            ingestion_service.complete_version_with_lease(
                version_id,
                lease_a,
                doc_content="STALE_OVERWRITE",
                chunk_count=999,
                session_factory=factory,
            )
            is False
        )

        # 5. 校验最终状态、文档激活与检索结果完全属于 Worker B
        with factory() as db:
            ver = db.get(DocumentVersion, version_id)
            doc = db.get(Document, doc_id)
            assert ver.status == "ready"
            assert ver.progress == 100
            assert ver.lease_owner == "ingest-b"
            assert ver.lease_token == lease_b.token
            assert ver.attempt == 2
            assert ver.lease_expires_at is None
            assert doc.active_version_id == version_id
            assert "ERR_T71_SCENE_A" in doc.content
            assert doc.chunk_count >= 1
            assert ver.index_config.get("activation_count") == 1
        hits = container.bm25.search("t-t71", "ERR_T71_SCENE_A", top_k=5, active_versions={doc_id: version_id})
        assert len(hits) == 1
    finally:
        eng.dispose()


def test_ingestion_scene_b_chunks_and_vectors_written_before_ready_crash(tmp_path):
    """入库任务场景 B：分块与向量/BM25 已落库但版本状态未提交 ready 即崩溃 -> 重领后幂等收口无重复分块与重复激活。"""
    db_url = "sqlite:///" + str(tmp_path / "ingest_scene_b.db")
    eng, factory = _make_isolated_db(db_url)
    try:
        container = _make_isolated_container(factory, lease_seconds=2.0)
        doc_id, version_id = _create_doc_and_version(
            factory,
            tmp_path,
            text_body="# 缓存击穿预案\n\nERR_T71_SCENE_B 需先检查 Redis 热点键。\n\n## 第二步\n\n开启本地短暂缓存并限流。",
        )

        # 模拟 Worker A 完成解析、分块落库、向量 upsert、BM25 写入，在提交 ready 之前崩溃
        t0 = _now()
        lease_a = ingestion_service.claim_version_by_id(
            version_id, "ingest-a", lease_seconds=2.0, now=t0, session_factory=factory
        )
        assert lease_a is not None
        with factory() as db:
            ver = db.get(DocumentVersion, version_id)
            doc = db.get(Document, doc_id)
            from app.services.document_parser import build_version_chunks, parse_file

            pages, warnings, parser = parse_file(ver.original_path, ver.filename, ver.parsed_path)
            ver.parser = parser
            parents, chunks = build_version_chunks(doc, ver, pages)
        assert (
            ingestion_service.advance_version_stage(
                version_id,
                lease_a,
                pages=pages,
                warnings=warnings,
                parser=parser,
                status="chunking",
                progress=30,
                session_factory=factory,
            )
            is True
        )
        assert (
            ingestion_service.persist_version_chunks_with_lease(
                version_id,
                lease_a,
                doc_id=doc_id,
                tenant_id="t-t71",
                parents=parents,
                chunks=chunks,
                index_config={"chunk_count": len(chunks)},
                session_factory=factory,
            )
            is True
        )
        vectors = container.embedder.embed([c["text"] for c in chunks])
        container.vector_store.upsert("t-t71", chunks, vectors)
        container.bm25.add("t-t71", chunks)

        expected_parents = len(parents)
        expected_chunks = len(chunks)
        assert expected_chunks >= 1
        with factory() as db:
            assert db.get(DocumentVersion, version_id).status == "indexing"
            assert db.get(Document, doc_id).active_version_id is None

        # 租约过期后 Worker B 重领并完整跑通收口
        lease_b = ingestion_service.claim_version_by_id(
            version_id,
            "ingest-b",
            lease_seconds=10.0,
            now=lease_a.expires_at + timedelta(seconds=1),
            session_factory=factory,
        )
        assert lease_b is not None and lease_b.attempt == 2
        assert ingestion_service.process_version(version_id, container, lease=lease_b, session_factory=factory) is True

        # 校验分块、父块、向量点、BM25 条目、版本激活全部严格唯一
        with factory() as db:
            ver = db.get(DocumentVersion, version_id)
            doc = db.get(Document, doc_id)
            assert ver.status == "ready"
            assert ver.attempt == 2
            assert ver.lease_owner == "ingest-b"
            assert ver.index_config["activation_count"] == 1
            assert doc.active_version_id == version_id
            assert doc.chunk_count == expected_chunks
            assert db.query(DocumentParent).filter_by(version_id=version_id).count() == expected_parents
            assert db.query(DocumentChunk).filter_by(version_id=version_id).count() == expected_chunks

        assert container.vector_store.count("t-t71") == expected_chunks
        assert container.bm25.count("t-t71") == expected_chunks
        # 再次尝试以旧或新 worker 领取已完成版本必须返回 None，不会重复激活
        assert ingestion_service.claim_version_by_id(version_id, "ingest-c", session_factory=factory) is None
    finally:
        eng.dispose()


def test_heartbeat_keeps_lease_alive_for_investigation_and_ingestion(tmp_path):
    """验证执行耗时超过单个租约 TTL 时，心跳续约维持租约有效，其他 worker 无法抢占且 attempt 保持为 1。"""
    db_url = "sqlite:///" + str(tmp_path / "heartbeat.db")
    eng, factory = _make_isolated_db(db_url)
    ckpt_conn = sqlite3.connect(tmp_path / "ckpt_hb.sqlite", check_same_thread=False)
    try:
        tenant_id, user_id, username, session_id = _seed_tenant_user_session(factory)
        mid_started = threading.Event()

        def slow_node(state):
            mid_started.set()
            time.sleep(0.45)
            return {"answer": "心跳保活完成", "end_reason": "answered"}

        sg = StateGraph(DiagState)
        sg.add_node("slow", slow_node)
        sg.add_edge(START, "slow")
        sg.add_edge("slow", END)
        compiled = sg.compile(checkpointer=SqliteSaver(ckpt_conn))

        class SlowEmbedder(MockEmbedding):
            def embed(self, texts):
                time.sleep(0.45)
                return super().embed(texts)

        container = _make_isolated_container(
            factory,
            graph=compiled,
            embedder=SlowEmbedder(dim=32),
            lease_seconds=0.25,
            heartbeat_interval=0.06,
        )

        # ---- 调查任务心跳保活 ----
        run_id = RunRecorder.start(
            container,
            tenant_id,
            session_id,
            "慢调查心跳测试",
            request_id="req-hb-1",
            background=True,
            queued=True,
            session_factory=factory,
        ).id
        lease_run = background_runs.claim_run_by_id(run_id, "worker-hb-run", lease_seconds=0.25, session_factory=factory)
        assert lease_run is not None
        initial_hb = lease_run.expires_at

        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(
                background_runs.execute,
                container,
                run_id,
                lease=lease_run,
                lease_seconds=0.25,
                session_factory=factory,
            )
            assert mid_started.wait(timeout=3.0)
            time.sleep(0.32)  # 超过初始 0.25s 租约，靠心跳持续续期
            assert background_runs.claim_run_by_id(run_id, "intruder", lease_seconds=1.0, session_factory=factory) is None
            assert fut.result(timeout=5.0) is True

        with factory() as db:
            run = db.get(AgentRun, run_id)
            assert run.status == "answered"
            assert run.attempt == 1
            assert run.lease_owner == "worker-hb-run"
            assert run.heartbeat_at is not None and run.heartbeat_at >= initial_hb - timedelta(seconds=0.25)

        # ---- 入库任务心跳保活 ----
        _, version_id = _create_doc_and_version(
            factory,
            tmp_path,
            text_body="# 慢入库测试\n\nERR_HB_INGEST 验证入库心跳续约。",
        )
        lease_ver = ingestion_service.claim_version_by_id(
            version_id, "worker-hb-ingest", lease_seconds=0.25, session_factory=factory
        )
        assert lease_ver is not None

        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(
                ingestion_service.process_version,
                version_id,
                container,
                lease=lease_ver,
                lease_seconds=0.25,
                session_factory=factory,
            )
            time.sleep(0.32)  # 超过初始 0.25s 租约
            assert (
                ingestion_service.claim_version_by_id(version_id, "intruder-ingest", lease_seconds=1.0, session_factory=factory)
                is None
            )
            assert fut.result(timeout=5.0) is True

        with factory() as db:
            ver = db.get(DocumentVersion, version_id)
            assert ver.status == "ready"
            assert ver.attempt == 1
            assert ver.lease_owner == "worker-hb-ingest"
    finally:
        ckpt_conn.close()
        eng.dispose()


def test_duplicate_submission_idempotency_and_concurrent_dual_worker_claiming(client, admin_headers, tmp_path):
    """验证重复投递同一 request_id/idempotency_key 幂等，以及双 worker 并发领取调查/入库任务零冲突。"""
    # 1. HTTP/队列重复投递同一 request_id：只创建一条 AgentRun，冲突内容返回 409
    session_id = new_session(client, admin_headers, "T71 重复投递验证")
    container = get_container()
    with container.db_session() as db:
        user_id = db.get(ChatSession, session_id).user_id
    auth_user = AuthUser(user_id=user_id, tenant_id="default", username="admin", role="admin")
    req_id = f"t71-dup-{uuid.uuid4().hex[:8]}"
    body = SimpleNamespace(session_id=session_id, message="排查 ERR_5003", request_id=req_id, workflow="auto", background=True)

    barrier = threading.Barrier(4)

    def _enqueue_same():
        barrier.wait()
        return background_runs.enqueue(container, auth_user, body)

    with ThreadPoolExecutor(max_workers=4) as pool:
        run_ids = list(pool.map(lambda _: _enqueue_same(), range(4)))
    assert len(set(run_ids)) == 1
    unique_run_id = run_ids[0]

    # 等待该后台任务自然跑完，核对结果和消息严格唯一
    def _read_status():
        with container.db_session() as db:
            row = db.get(AgentRun, unique_run_id)
            return row.status if row else None

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        st = run_with_sqlite_retry(_read_status)
        if st and st not in background_runs.ACTIVE:
            break
        time.sleep(0.05)

    def _verify_run_and_msgs():
        with container.db_session() as db:
            assert db.query(AgentRun).filter_by(session_id=session_id, request_id=req_id).count() == 1
            msgs = db.query(Message).filter_by(session_id=session_id).order_by(Message.id).all()
            assert [m.role for m in msgs] == ["user", "assistant"]

    run_with_sqlite_retry(_verify_run_and_msgs)

    # 同一 request_id 改换不同问题必须明确拒绝 409
    conflict_body = SimpleNamespace(
        session_id=session_id, message="完全不同的问题", request_id=req_id, workflow="auto", background=True
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        background_runs.enqueue(container, auth_user, conflict_body)
    assert exc_info.value.status_code == 409

    # 2. 独立库：双 worker 并发领取 6 条调查任务 + 6 条入库任务，验证完全不冲突、不漏领、不重复
    db_url = "sqlite:///" + str(tmp_path / "concurrent_claim.db")
    eng, factory = _make_isolated_db(db_url)
    try:
        iso_container = _make_isolated_container(factory, lease_seconds=30.0)
        iso_container.settings.max_concurrent_runs = 20
        expected_runs = set()
        for i in range(6):
            tid, _, _, sid = _seed_tenant_user_session(factory, tenant_id="t-conc", user_id="u-conc", username="u_conc")
            rec = RunRecorder.start(
                iso_container,
                tid,
                sid,
                f"并发调查-{i}",
                request_id=f"conc-run-{i}",
                background=True,
                queued=True,
                session_factory=factory,
            )
            expected_runs.add(rec.id)

        expected_versions = set()
        for i in range(6):
            _, vid = _create_doc_and_version(
                factory,
                tmp_path,
                tenant_id="t-conc",
                text_body=f"# 并发文档 {i}\n\n内容 {i}",
                filename=f"doc_{i}.md",
                idempotency_key=f"conc-ingest-key-{i}",
            )
            expected_versions.add(vid)

        claim_barrier = threading.Barrier(2)

        def _claim_all_runs(worker_name: str) -> list[str]:
            claim_barrier.wait()
            claimed = []
            while True:
                lease = background_runs.claim_next_run(worker_name, lease_seconds=30.0, session_factory=factory)
                if lease is None:
                    break
                claimed.append(lease.task_id)
            return claimed

        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(_claim_all_runs, "run-worker-a")
            fb = pool.submit(_claim_all_runs, "run-worker-b")
            claimed_a, claimed_b = fa.result(), fb.result()

        assert len(claimed_a) + len(claimed_b) == 6
        assert set(claimed_a).isdisjoint(set(claimed_b))
        assert set(claimed_a) | set(claimed_b) == expected_runs

        ver_barrier = threading.Barrier(2)

        def _claim_all_versions(worker_name: str) -> list[str]:
            ver_barrier.wait()
            claimed = []
            while True:
                lease = ingestion_service.claim_next_version(worker_name, lease_seconds=30.0, session_factory=factory)
                if lease is None:
                    break
                claimed.append(lease.task_id)
            return claimed

        with ThreadPoolExecutor(max_workers=2) as pool:
            va = pool.submit(_claim_all_versions, "ingest-worker-a")
            vb = pool.submit(_claim_all_versions, "ingest-worker-b")
            v_claimed_a, v_claimed_b = va.result(), vb.result()

        assert len(v_claimed_a) + len(v_claimed_b) == 6
        assert set(v_claimed_a).isdisjoint(set(v_claimed_b))
        assert set(v_claimed_a) | set(v_claimed_b) == expected_versions

        # 入库重复 enqueue：已就绪或已有有效租约的版本不会被重复重置
        sample_vid = next(iter(expected_versions))
        with factory() as db:
            row = db.get(DocumentVersion, sample_vid)
            assert row.attempt == 1
            original_owner = row.lease_owner
            original_token = row.lease_token
        assert ingestion_service.enqueue(sample_vid, iso_container, session_factory=factory) == sample_vid
        with factory() as db:
            row = db.get(DocumentVersion, sample_vid)
            assert row.lease_owner == original_owner
            assert row.lease_token == original_token
    finally:
        eng.dispose()


def test_post_finish_heartbeat_and_trailing_token_buffer_do_not_false_lease_lost(tmp_path, monkeypatch, caplog):
    """回归：终态提交后的心跳续约与未 flush 尾 token 缓冲均不得误判失租或写入已终态 Run。"""
    from app.services.run_service import current_run

    db_url = "sqlite:///" + str(tmp_path / "post_finish_race.db")
    eng, factory = _make_isolated_db(db_url)
    try:
        # ---- 场景 1：finish() 后心跳线程在 yield final/done 前醒来，不误置 lease_lost ----
        tid, _, _, sid1 = _seed_tenant_user_session(factory, tenant_id="t-pf", user_id="u-pf", username="u_pf")
        container = _make_isolated_container(factory, lease_seconds=0.12, heartbeat_interval=0.02)

        run1 = RunRecorder.start(
            container,
            tid,
            sid1,
            "验证终态后心跳竞态",
            request_id="req-pf-hb",
            background=True,
            queued=True,
            session_factory=factory,
        ).id

        def _stream_finish_then_sleep(*args, **kwargs):
            yield {"type": "node_status", "node": "generate", "status": "completed"}
            current_run().finish({"status": "answered", "end_reason": "answered", "answer": "ok"})
            # 让心跳线程在 finish() 之后、消费 final/done 之前明确醒来触发 renew
            time.sleep(0.08)
            yield {"type": "final", "answer": "ok"}
            yield {"type": "done"}

        monkeypatch.setattr(agent_service, "_chat_stream", _stream_finish_then_sleep)
        caplog.clear()
        with caplog.at_level("WARNING"):
            done_id = background_runs.run_once(
                container,
                worker_id="worker-pf-1",
                run_id=run1,
                lease_seconds=0.12,
                session_factory=factory,
            )
        assert done_id == run1
        assert "background_run_lease_lost" not in caplog.text
        with factory() as db:
            row = db.get(AgentRun, run1)
            assert row.status == "answered"
            assert row.data["result"]["answer"] == "ok"

        # ---- 场景 2：尾 token 缓冲未 flush 即在流内 finish(budget_refused) 并 yield error，不触发 LeaseLostError ----
        _, _, _, sid2 = _seed_tenant_user_session(factory, tenant_id="t-pf", user_id="u-pf", username="u_pf")
        run2 = RunRecorder.start(
            container,
            tid,
            sid2,
            "验证尾 token 缓冲与终态竞争",
            request_id="req-pf-tok",
            background=True,
            queued=True,
            session_factory=factory,
        ).id

        def _stream_trailing_token_then_finish(*args, **kwargs):
            # 短 token 留在 background_runs 缓冲中（<0.25s 且 <1024B）
            yield {"type": "token", "delta": "半截输出"}
            # 模拟 BudgetExceededError：流内先 finish 再 yield error
            current_run().finish({"status": "budget_refused", "error": "预算不足"})
            yield {"type": "error", "error_type": "budget_exceeded", "message": "预算不足"}

        monkeypatch.setattr(agent_service, "_chat_stream", _stream_trailing_token_then_finish)
        caplog.clear()
        with caplog.at_level("WARNING"):
            done_id_2 = background_runs.run_once(
                container,
                worker_id="worker-pf-2",
                run_id=run2,
                lease_seconds=0.12,
                session_factory=factory,
            )
        assert done_id_2 == run2
        assert "background_run_lease_lost" not in caplog.text
        with factory() as db:
            row = db.get(AgentRun, run2)
            assert row.status == "budget_refused"
    finally:
        eng.dispose()


@pytest.mark.skipif(not os.getenv("OPSPILOT_TEST_PG_URL"), reason="未配置 OPSPILOT_TEST_PG_URL，跳过真实 PostgreSQL 16 并发实测")
def test_postgres_skip_locked_concurrent_claim_and_recovery(tmp_path):
    """真实 PostgreSQL 16 实测：验证 FOR UPDATE SKIP LOCKED 非阻塞跳过、双 worker 并发领取、心跳续约与过期拒绝。"""
    pg_url = os.environ["OPSPILOT_TEST_PG_URL"]
    eng, factory = _make_isolated_db(pg_url)
    try:
        iso_container = _make_isolated_container(factory, lease_seconds=2.0)
        iso_container.settings.max_concurrent_runs = 20

        # 1. 验证 FOR UPDATE SKIP LOCKED 真实行为：会话 1 锁住第 1 条任务未提交时，会话 2 直接跳过并领走第 2 条！
        tid, _, _, sid1 = _seed_tenant_user_session(factory, tenant_id="t-pg", user_id="u-pg", username="u_pg")
        _, _, _, sid2 = _seed_tenant_user_session(factory, tenant_id="t-pg", user_id="u-pg", username="u_pg")
        r1 = RunRecorder.start(
            iso_container, tid, sid1, "PG 任务 1", request_id=f"pg-r1-{uuid.uuid4().hex[:6]}", background=True, queued=True, session_factory=factory
        ).id
        r2 = RunRecorder.start(
            iso_container, tid, sid2, "PG 任务 2", request_id=f"pg-r2-{uuid.uuid4().hex[:6]}", background=True, queued=True, session_factory=factory
        ).id

        with factory() as holder_db:
            # 手工模拟 Worker 1 正在事务内持锁第 1 条任务尚未 commit
            locked_row = (
                holder_db.query(AgentRun)
                .filter_by(id=r1)
                .with_for_update()
                .one()
            )
            assert locked_row.id == r1
            # Worker 2 并发调用 claim_next_run：因为 SKIP LOCKED，绝不阻塞，直接领走 r2！
            lease2 = background_runs.claim_next_run("pg-worker-2", lease_seconds=5.0, session_factory=factory)
            assert lease2 is not None
            assert lease2.task_id == r2
            holder_db.rollback()

        # 解锁后 Worker 1 领走 r1
        lease1 = background_runs.claim_next_run("pg-worker-1", lease_seconds=1.0, session_factory=factory)
        assert lease1 is not None and lease1.task_id == r1

        # 2. 验证 PG 下租约过期重领与旧 worker 提交拒绝
        lease1_takeover = background_runs.claim_run_by_id(
            r1,
            "pg-worker-takeover",
            lease_seconds=10.0,
            now=lease1.expires_at + timedelta(seconds=1),
            session_factory=factory,
        )
        assert lease1_takeover is not None and lease1_takeover.attempt == 2
        assert background_runs.finish_run_with_lease(r1, tid, lease1, {"status": "failed"}, session_factory=factory) is False
        assert (
            background_runs.finish_run_with_lease(
                r1, tid, lease1_takeover, {"status": "answered", "end_reason": "answered", "answer": "pg-ok"}, session_factory=factory
            )
            is True
        )
        assert (
            background_runs.finish_run_with_lease(r2, tid, lease2, {"status": "answered", "end_reason": "answered", "answer": "pg-ok-2"}, session_factory=factory)
            is True
        )

        # 3. 验证 PG 下入库任务双 worker 并发 SKIP LOCKED 领取与恢复
        vids = set()
        for i in range(4):
            _, vid = _create_doc_and_version(
                factory,
                tmp_path,
                tenant_id="t-pg",
                text_body=f"# PG 文档 {i}\n\nERR_PG_{i} 内容",
                filename=f"pg_{i}.md",
                idempotency_key=f"pg-ingest-{uuid.uuid4().hex[:8]}-{i}",
            )
            vids.add(vid)

        barrier = threading.Barrier(2)

        def _pg_claim_versions(owner: str) -> list[str]:
            barrier.wait()
            out = []
            while True:
                lease = ingestion_service.claim_next_version(owner, lease_seconds=5.0, session_factory=factory)
                if lease is None:
                    break
                out.append(lease.task_id)
            return out

        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(_pg_claim_versions, "pg-ingest-a")
            fb = pool.submit(_pg_claim_versions, "pg-ingest-b")
            ca, cb = fa.result(), fb.result()

        assert len(ca) + len(cb) == 4
        assert set(ca).isdisjoint(set(cb))
        assert set(ca) | set(cb) == vids
    finally:
        eng.dispose()

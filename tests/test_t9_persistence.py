"""文件检查点重开、提交后中断的幂等恢复与无损迁移；模型/业务库为离线夹具。"""

import sqlite3
from dataclasses import replace

import pytest
from fastapi import HTTPException
from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.agent.graph import build_graph
from app.agent.nodes import DiagnosisNodes
from app.auth.deps import AuthUser
from app.container import get_container
from app.db.base import SessionLocal
from app.db.models import ChatSession, Ticket
from app.services import agent_service, ticket_service
from tests.conftest import new_session


def _container(path):
    conn = sqlite3.connect(str(path), check_same_thread=False)
    c = replace(get_container(), checkpoint_connection=conn)
    nodes = DiagnosisNodes(c)
    c.graph = build_graph(nodes, SqliteSaver(conn))
    return c


def _start(c, client, headers):
    sid = new_session(client, headers, "T9离线恢复")
    with SessionLocal() as db:
        user_id = db.get(ChatSession, sid).user_id
        result = agent_service.chat_once(c, db, "default", user_id, "admin", sid, "订单服务超时，请创建工单")
    return sid, result["approval"]["approval_id"]


AUTH = AuthUser(user_id="test", username="admin", tenant_id="default", role="admin")


@pytest.mark.parametrize("approved", [True, False])
def test_reopen_checkpoint_then_decide_and_retry(tmp_path, client, admin_headers, approved):
    path = tmp_path / "checkpoints.sqlite3"
    c = _container(path)
    sid, approval_id = _start(c, client, admin_headers)
    c.checkpoint_connection.close()
    restored = _container(path)
    try:
        with SessionLocal() as db:
            first = agent_service.decide_approval(restored, db, AUTH, approval_id, approved, "首次决定")
            repeated = agent_service.decide_approval(restored, db, AUTH, approval_id, approved, "重复不能改写首次决定")
            assert repeated["ticket_id"] == first["ticket_id"]
            assert bool(first["ticket_id"]) == approved
            assert db.query(Ticket).filter(Ticket.approval_id == approval_id).count() == int(approved)
            with pytest.raises(HTTPException) as exc:
                agent_service.decide_approval(restored, db, AUTH, approval_id, not approved)
            assert exc.value.status_code == 409
        assert not restored.graph.get_state({"configurable": {"thread_id": sid}}).next
    finally:
        restored.checkpoint_connection.close()


def test_replay_after_ticket_committed_before_graph_checkpoint(tmp_path, client, admin_headers, monkeypatch):
    path = tmp_path / "crash.sqlite3"
    c = _container(path)
    sid, approval_id = _start(c, client, admin_headers)
    original = ticket_service.create_ticket

    def crash_after_commit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("模拟工单提交后、图检查点写入前中断")

    monkeypatch.setattr(ticket_service, "create_ticket", crash_after_commit)
    with SessionLocal() as db, pytest.raises(RuntimeError, match="工单提交后"):
        agent_service.decide_approval(c, db, AUTH, approval_id, True)
    c.checkpoint_connection.close()
    monkeypatch.setattr(ticket_service, "create_ticket", original)
    restored = _container(path)
    try:
        with SessionLocal() as db:
            result = agent_service.decide_approval(restored, db, AUTH, approval_id, True)
            assert result["ticket_id"]
            assert db.query(Ticket).filter(Ticket.approval_id == approval_id).count() == 1
        assert not restored.graph.get_state({"configurable": {"thread_id": sid}}).next
    finally:
        restored.checkpoint_connection.close()


def test_legacy_ticket_migration_preserves_rows_and_adds_unique_constraint(tmp_path, monkeypatch):
    from app.db import base

    old = create_engine("sqlite:///" + str(tmp_path / "legacy.sqlite3"))
    with old.begin() as conn:
        conn.execute(
            text("""CREATE TABLE tickets (
            id VARCHAR(64) PRIMARY KEY,tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',session_id VARCHAR(64) NOT NULL DEFAULT '',
            title VARCHAR(256) NOT NULL,description TEXT NOT NULL DEFAULT '',severity VARCHAR(16) NOT NULL DEFAULT 'P2',
            status VARCHAR(16) NOT NULL DEFAULT 'open',created_by VARCHAR(64) NOT NULL DEFAULT '',approver VARCHAR(64) NOT NULL DEFAULT '',
            decision_comment VARCHAR(512) NOT NULL DEFAULT '',created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,decided_at DATETIME NULL
        )""")
        )
        conn.execute(text("INSERT INTO tickets (id,title) VALUES ('legacy','原工单')"))
    monkeypatch.setattr(base, "engine", old)
    base.init_db()
    base.init_db()
    with old.begin() as conn:
        assert conn.execute(text("SELECT title,approval_id FROM tickets WHERE id='legacy'")).one() == ("原工单", None)
        conn.execute(text("INSERT INTO tickets(id,title,approval_id) VALUES ('new','新工单','approval-1')"))
    with pytest.raises(IntegrityError), old.begin() as conn:
        conn.execute(text("INSERT INTO tickets(id,title,approval_id) VALUES ('dup','重复工单','approval-1')"))
    old.dispose()


def test_checkpoint_connection_closes_on_container_reset(tmp_path, monkeypatch):
    from app import container as container_module

    c = _container(tmp_path / "close.sqlite3")
    monkeypatch.setattr(container_module, "_container", c)
    container_module.reset_container()
    with pytest.raises(sqlite3.ProgrammingError):
        c.checkpoint_connection.execute("SELECT 1")

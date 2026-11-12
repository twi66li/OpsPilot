"""工作区清理与入口权限回归；不调用模型。"""
from app.db.base import SessionLocal
from app.db.models import AgentRun, ApprovalRequest, ChatSession, Message, RemediationRequest, Ticket
from tests.conftest import new_session, register


def test_delete_sessions_is_owned_atomic_and_preserves_audit(client, admin_headers, viewer_headers):
    sid = new_session(client, admin_headers, "待清理演练")
    other = new_session(client, viewer_headers, "另一用户的会话")
    with SessionLocal() as db:
        session = db.get(ChatSession, sid)
        db.add(Message(session_id=sid, tenant_id=session.tenant_id, role="user", content="原证据"))
        ticket = Ticket(tenant_id=session.tenant_id, session_id=sid, title="保留审批关联")
        db.add(ticket)
        db.add(AgentRun(id="cleanup-run", tenant_id=session.tenant_id, session_id=sid, mode="native", data={}))
        approval = ApprovalRequest(tenant_id=session.tenant_id, session_id=sid, ticket_draft={})
        repair = RemediationRequest(tenant_id=session.tenant_id, run_id="cleanup-run", action="reset_demo_fault", reason="测试", requested_by="admin")
        db.add_all([approval, repair])
        db.commit()
        ticket_id = ticket.id
        approval_id, repair_id = approval.id, repair.id
    assert client.post("/api/sessions/delete", json={"ids": [sid, other]}, headers=admin_headers).status_code == 404
    assert client.get(f"/api/sessions/{sid}", headers=admin_headers).status_code == 200
    assert client.delete(f"/api/sessions/{sid}", headers=viewer_headers).status_code == 404
    assert client.post("/api/sessions/delete", json={"ids": [sid]}, headers=admin_headers).json() == {"deleted": 1}
    assert client.get(f"/api/sessions/{sid}", headers=admin_headers).status_code == 404
    assert sid not in {s["id"] for s in client.get("/api/sessions", headers=admin_headers).json()}
    assert client.post("/api/agent/chat", json={"session_id": sid, "message": "继续"}, headers=admin_headers).status_code == 404
    assert client.get("/api/agent/runs/cleanup-run", headers=admin_headers).status_code == 404
    assert client.post(f"/api/agent/approvals/{approval_id}/decide", json={"approved":True}, headers=admin_headers).status_code == 409
    with SessionLocal() as db:
        assert db.get(Ticket, ticket_id) is not None
        assert db.query(Message).filter_by(session_id=sid).count() == 1
        assert db.get(ApprovalRequest, approval_id).status == "cancelled"
        assert db.get(RemediationRequest, repair_id).status == "cancelled"


def test_delete_website_preserve_or_delete_related_cases(client, admin_headers, viewer_headers):
    for delete_records in (False, True):
        site = client.post("/api/support/sites", json={"name": "删除行为测试"}, headers=admin_headers).json()
        case = client.post("/api/support/cases", json={"title": "关联问题", "site_id": site["id"]}, headers=admin_headers).json()
        path = f"/api/support/sites/{site['id']}?delete_records={str(delete_records).lower()}"
        assert client.delete(path, headers=viewer_headers).status_code == 404
        assert client.delete(path, headers=admin_headers).status_code == 200
        assert client.get(f"/api/support/sites/{site['id']}", headers=admin_headers).status_code == 404
        assert site["id"] not in {s["id"] for s in client.get("/api/support/sites", headers=admin_headers).json()}
        response = client.get(f"/api/support/cases/{case['id']}", headers=admin_headers)
        if delete_records:
            assert response.status_code == 404
            assert case["id"] not in {c["id"] for c in client.get("/api/support/cases", headers=admin_headers).json()}
        else:
            assert response.status_code == 200
            assert response.json()["site_id"] is None
            assert response.json()["profile"]["name"] == "删除行为测试"
            assert client.delete(f"/api/support/cases/{case['id']}", headers=admin_headers).status_code == 200


def test_registration_has_private_workspace_without_admin_privileges(client):
    response = client.post("/auth/register", json={"username":"workspace_new_user", "password":"pass123456", "role":"admin", "tenant_id":"default"})
    auth = response.json()
    assert response.status_code == 200 and auth["role"] == "viewer" and auth["tenant_id"] != "default"
    headers = {"Authorization":"Bearer " + auth["access_token"]}
    assert client.get("/api/support/sites", headers=headers).json() == []
    assert client.get("/api/support/cases", headers=headers).json() == []
    assert client.get("/api/sessions", headers=headers).json() == []
    assert client.post("/api/support/sites", json={"name":"我的第一个网站"}, headers=headers).status_code == 200
    assert client.get("/api/eval/runs", headers=headers).status_code == 403
    assert client.post("/api/admin/users", json={"username":"unauthorized_admin", "password":"pass123456", "role":"admin"}, headers=headers).status_code == 403
    other = register(client, "workspace_other_user", "pass123456")
    assert client.get("/api/support/sites", headers=other).json() == []

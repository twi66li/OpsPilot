"""工单人工审批（LangGraph interrupt / resume）测试。"""
from tests.conftest import new_session


def _start_ticket_flow(client, headers, session_id: str, message: str = "订单服务又超时了，请帮我创建工单跟进处理。") -> dict:
    resp = client.post("/api/agent/chat", json={"session_id": session_id, "message": message}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_ticket_flow_requires_approval(client, admin_headers):
    session_id = new_session(client, admin_headers)
    result = _start_ticket_flow(client, admin_headers, session_id)

    assert result["status"] == "approval_required"
    assert result["intent"] == "create_ticket"
    approval = result["approval"]
    assert approval["approval_id"]
    draft = approval["ticket_draft"]
    assert draft["title"] and draft["severity"] in ("P1", "P2", "P3")

    pending = client.get(f"/api/agent/sessions/{session_id}/pending-approval", headers=admin_headers).json()
    assert pending["pending"] is True
    assert pending["approval_id"] == approval["approval_id"]


def test_viewer_cannot_decide(client, admin_headers, viewer_headers):
    session_id = new_session(client, admin_headers)
    result = _start_ticket_flow(client, admin_headers, session_id)
    resp = client.post(
        f"/api/agent/approvals/{result['approval']['approval_id']}/decide",
        json={"approved": True, "comment": "viewer 尝试审批"},
        headers=viewer_headers,
    )
    assert resp.status_code == 403


def test_approve_creates_ticket(client, admin_headers, engineer_headers):
    session_id = new_session(client, admin_headers)
    result = _start_ticket_flow(client, admin_headers, session_id)
    approval_id = result["approval"]["approval_id"]

    resp = client.post(
        f"/api/agent/approvals/{approval_id}/decide",
        json={"approved": True, "comment": "同意创建，尽快处理"},
        headers=engineer_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["approval_status"] == "approved"
    assert body["ticket_id"]

    tickets = client.get("/api/tickets", headers=admin_headers).json()
    ticket = next(t for t in tickets if t["id"] == body["ticket_id"])
    assert ticket["status"] == "open"
    assert ticket["approver"] == "engineer_e2e"

    detail = client.get(f"/api/tickets/{body['ticket_id']}", headers=admin_headers).json()
    assert "order-service" in detail["title"] or "超时" in detail["title"]
    assert "同意创建" in detail["decision_comment"] or detail["decision_comment"] == ""


def test_reject_does_not_create_ticket(client, admin_headers, engineer_headers):
    before = {t["id"] for t in client.get("/api/tickets", headers=admin_headers).json()}
    session_id = new_session(client, admin_headers)
    result = _start_ticket_flow(client, admin_headers, session_id, "支付服务对账失败，帮我提交工单。")
    approval_id = result["approval"]["approval_id"]

    resp = client.post(
        f"/api/agent/approvals/{approval_id}/decide",
        json={"approved": False, "comment": "信息不足，先补充日志"},
        headers=engineer_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["approval_status"] == "rejected"
    assert not body["ticket_id"]
    assert "拒绝" in body["answer"]

    after = {t["id"] for t in client.get("/api/tickets", headers=admin_headers).json()}
    assert after == before


def test_repeated_same_decision_returns_existing_ticket(client, admin_headers, engineer_headers):
    session_id = new_session(client, admin_headers)
    result = _start_ticket_flow(client, admin_headers, session_id)
    approval_id = result["approval"]["approval_id"]
    first = client.post(
        f"/api/agent/approvals/{approval_id}/decide", json={"approved": True, "comment": ""}, headers=engineer_headers
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/agent/approvals/{approval_id}/decide", json={"approved": True, "comment": ""}, headers=engineer_headers
    )
    assert second.status_code == 200
    assert second.json()["ticket_id"] == first.json()["ticket_id"]

"""审批前不执行；批准后只执行一次；实际演练请求验证与租户/归属约束。"""
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.services import remediation_service
from scripts import metrics_demo
from tests.conftest import new_session, seed_tenant_admin


@pytest.fixture
def demo_service(monkeypatch):
    monkeypatch.setattr(get_settings(), "remediation_enabled", True)
    demo = TestClient(metrics_demo.app)
    demo.post("/control", json={"error_every": 1})
    original_client = httpx.Client

    def handle(request):
        response = demo.request(request.method, request.url.path, content=request.content, headers=dict(request.headers))
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    monkeypatch.setattr(remediation_service.httpx, "Client", lambda **_: original_client(
        base_url="http://demo", transport=httpx.MockTransport(handle)))
    yield demo
    demo.post("/control", json={"error_every": 0})
    demo.close()


def completed_run(client, headers):
    session_id = new_session(client, headers)
    response = client.post("/api/agent/chat", headers=headers,
                           json={"session_id": session_id, "message": "ERR_5003 连接池耗尽如何排查"})
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


def request_repair(client, headers, run_id):
    return client.post("/api/remediations", headers=headers,
                       json={"run_id": run_id, "reason": "申请关闭测试故障注入并核对请求结果"})


def test_rejection_never_executes_and_viewer_cannot_approve(client, admin_headers, viewer_headers, demo_service):
    row = request_repair(client, admin_headers, completed_run(client, admin_headers)).json()
    assert row["status"] == "pending"
    assert demo_service.get("/control").json()["fault_injection"]["error_every"] == 1
    url = f"/api/remediations/{row['id']}/decide"
    assert client.post(url, headers=viewer_headers, json={"approved": True}).status_code == 403
    rejected = client.post(url, headers=admin_headers, json={"approved": False}).json()
    assert rejected["status"] == "rejected" and not rejected["result"]
    assert demo_service.get("/control").json()["fault_injection"]["error_every"] == 1
    assert client.post(url, headers=admin_headers, json={"approved": True}).status_code == 409


def test_approved_action_verifies_actual_requests_and_is_idempotent(client, admin_headers, demo_service):
    run_id = completed_run(client, admin_headers)
    row = request_repair(client, admin_headers, run_id).json()
    url = f"/api/remediations/{row['id']}/decide"
    result = client.post(url, headers=admin_headers, json={"approved": True, "comment": "批准演练"}).json()
    assert result["status"] == "succeeded", result
    assert {s["status_code"] for s in result["result"]["before"]["samples"]} == {500}
    assert {s["status_code"] for s in result["result"]["after"]["samples"]} == {200}
    assert result["result"]["after"]["healthy"]
    count = len(metrics_demo.request_logs)
    assert client.post(url, headers=admin_headers, json={"approved": True}).json()["status"] == "succeeded"
    assert request_repair(client, admin_headers, run_id).json()["id"] == row["id"]
    assert len(metrics_demo.request_logs) == count, "重复批准不得再次执行或探测"


def test_request_is_bound_to_owned_run_and_tenant(client, admin_headers, engineer_headers, demo_service):
    run_id = completed_run(client, admin_headers)
    assert request_repair(client, engineer_headers, run_id).status_code == 404
    other = seed_tenant_admin(client, uuid.uuid4().hex, "repair_" + uuid.uuid4().hex[:8], "pass123456")
    row = request_repair(client, admin_headers, run_id).json()
    assert request_repair(client, other, run_id).status_code == 404
    assert client.get("/api/remediations", headers=other).json() == []
    assert client.post(f"/api/remediations/{row['id']}/decide", headers=other, json={"approved": True}).status_code == 404

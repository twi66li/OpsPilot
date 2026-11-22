import pytest

from app.container import get_container
from app.db.base import SessionLocal
from app.db.models import AgentRun, AgentRunEvent
from app.services.run_service import RunCancelled, RunRecorder, capture_run, check_cancelled
from tests.conftest import new_session


def test_request_id_reuses_results_and_events_are_appended(client, admin_headers):
    sid = new_session(client, admin_headers, "幂等调用检查")
    body = {"session_id": sid, "message": "你好", "request_id": "request-same-001"}
    first = client.post("/api/agent/chat", json=body, headers=admin_headers)
    assert first.status_code == 200, first.text
    second = client.post("/api/agent/chat", json=body, headers=admin_headers)
    assert second.status_code == 200 and second.json()["run_id"] == first.json()["run_id"]
    messages = client.get(f"/api/sessions/{sid}", headers=admin_headers).json()["messages"]
    assert len(messages) == 2
    assert (
        client.post("/api/agent/chat", json={**body, "message": "另一个问题"}, headers=admin_headers).status_code == 409
    )
    run_id = first.json()["run_id"]
    with SessionLocal() as db:
        assert db.get(AgentRun, run_id).data["events"] == []
        assert db.query(AgentRunEvent).filter_by(run_id=run_id).count() > 0
    assert client.get(f"/api/agent/runs/{run_id}", headers=admin_headers).json()["data"]["events"]


def test_cancellation_and_one_active_run_per_session(client, admin_headers, viewer_headers):
    sid = new_session(client, admin_headers, "可取消运行")
    recorder = RunRecorder.start(get_container(), "default", sid, "等待取消", request_id="active-001")
    with pytest.raises(Exception) as caught:
        RunRecorder.start(get_container(), "default", sid, "重复执行", request_id="active-002")
    assert getattr(caught.value, "status_code", None) == 409
    assert client.post(f"/api/agent/runs/{recorder.id}/cancel", headers=viewer_headers).status_code == 404
    assert (
        client.post(f"/api/agent/runs/{recorder.id}/cancel", headers=admin_headers).json()["status"]
        == "cancel_requested"
    )
    with capture_run(recorder), pytest.raises(RunCancelled):
        check_cancelled()
    recorder.finish({"status": "cancelled", "end_reason": "user_cancelled"})
    assert client.get(f"/api/agent/runs/{recorder.id}", headers=admin_headers).json()["status"] == "cancelled"


def test_guided_collection_and_log_preview_are_read_only(client, admin_headers):
    assert (
        client.post(
            "/api/maintenance/collection-guide",
            json={"scenario": "pool", "deployment": "unknown"},
            headers=admin_headers,
        ).json()["steps"][0]["command"]
        == ""
    )
    guide = client.post(
        "/api/maintenance/collection-guide",
        json={"scenario": "container", "deployment": "docker", "target": "orders-api"},
        headers=admin_headers,
    ).json()
    assert "--timestamps" in guide["steps"][0]["command"] and "orders-api" in guide["steps"][0]["command"]
    assert (
        client.post(
            "/api/maintenance/collection-guide",
            json={"scenario": "pool", "deployment": "docker", "target": "x; whoami"},
            headers=admin_headers,
        ).status_code
        == 400
    )
    preview = client.post(
        "/api/maintenance/evidence/file-preview",
        files={"file": ("error.log", b"password=private-value\nGET /orders HTTP 500")},
        headers=admin_headers,
    )
    assert preview.status_code == 200 and "private-value" not in preview.json()["text"]
    assert (
        client.post(
            "/api/maintenance/evidence/file-preview", files={"file": ("huge.log", b"x" * 65537)}, headers=admin_headers
        ).status_code
        == 413
    )


def test_problem_timeline_export_and_server_search(client, admin_headers, viewer_headers):
    site = client.post("/api/support/sites", json={"name": "维护升级测试"}, headers=admin_headers).json()
    assert client.get("/api/workspace/sites?q=维护升级测试", headers=admin_headers).json()["total"] == 1
    ids = []
    for i in range(3):
        case = client.post(
            "/api/support/cases",
            json={"title": f"paged-upgrade-unique-{i}", "site_id": site["id"]},
            headers=admin_headers,
        ).json()
        ids.append(case["id"])
    page = client.get("/api/workspace/cases?q=paged-upgrade-unique&page=2&page_size=2", headers=admin_headers).json()
    assert page["total"] == 3 and len(page["items"]) == 1 and page["page"] == 2
    assert client.get("/api/workspace/cases?q=paged-upgrade-unique", headers=viewer_headers).json()["total"] == 0
    cid = ids[0]
    assert (
        client.put(
            f"/api/maintenance/cases/{cid}/metadata",
            json={"severity": "P1", "labels": ["发布后"]},
            headers=admin_headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/maintenance/cases/{cid}/comments",
            json={"text": "检查记录 token=private-token"},
            headers=admin_headers,
        ).status_code
        == 200
    )
    client.post(
        f"/api/support/cases/{cid}/evidence",
        json={"text": "GET /orders HTTP 500", "label": "应用日志"},
        headers=admin_headers,
    )
    events = client.get(f"/api/maintenance/cases/{cid}/timeline", headers=admin_headers).json()["events"]
    assert {"case_created", "metadata_changed", "comment", "evidence_submitted"} <= {e["kind"] for e in events}
    report = client.get(f"/api/maintenance/cases/{cid}/report", headers=admin_headers)
    assert report.status_code == 200 and "P1" in report.text and "private-token" not in report.text
    assert client.get(f"/api/maintenance/cases/{cid}/report", headers=viewer_headers).status_code == 404
    assert client.get(f"/api/maintenance/sites/{site['id']}/timeline", headers=admin_headers).json()["events"]
    assert client.get("/api/maintenance/todos", headers=admin_headers).status_code == 200

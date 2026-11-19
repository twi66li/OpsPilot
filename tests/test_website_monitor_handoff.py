from datetime import datetime, timedelta

from app.services import website_monitor
from tests.conftest import admin_create_user, register


def test_monitor_opens_one_case_and_keeps_it_for_manual_review(client, admin_headers, monkeypatch):
    created = client.post("/api/support/sites", headers=admin_headers,
                          json={"name": "结算巡检", "url": "https://shop.example.test/orders?secret=do-not-send"})
    site_id = created.json()["id"]
    path = f"/api/maintenance/sites/{site_id}/http-monitor"
    now = datetime(2026, 11, 18, 1)
    states = iter([502, 502, 502, 502, 200, 503, 503, 503])

    def probe(url):
        assert url == "https://shop.example.test/orders"
        status = next(states)
        return {"state": "normal" if status == 200 else "abnormal", "http_status": status,
                "collected_at": now.isoformat() + "Z", "source_url": url}

    monkeypatch.setattr(website_monitor, "_now", lambda: now)
    monkeypatch.setattr(website_monitor, "probe_url", probe)
    enabled = client.put(path, headers=admin_headers, json={"enabled": True, "interval_minutes": 5})
    assert enabled.status_code == 200, enabled.text
    other = register(client, "monitor-other-workspace", "monitor-other-secret")
    assert client.put(path, headers=other, json={"enabled": False}).status_code == 404

    def run_checks(count):
        nonlocal now
        for _ in range(count):
            website_monitor.run_due_checks()
            now += timedelta(minutes=5)
        return client.get(f"/api/support/sites/{site_id}", headers=admin_headers).json()

    assert run_checks(2)["cases"] == []
    first = run_checks(2)
    assert len(first["cases"]) == 1
    case_id = first["http_monitor"]["case_id"]
    case = client.get(f"/api/support/cases/{case_id}", headers=admin_headers).json()
    assert case["status"] == "evidence_submitted"
    assert case["evidence"][0]["source_kind"] == "scheduled_http_probe"
    assert case["evidence"][0]["text"].count("502") == 3
    activities = client.get("/api/maintenance/activity", headers=admin_headers).json()["events"]
    assert any(row["kind"] == "http_incident" and row["case_id"] == case_id for row in activities)
    recovered = run_checks(1)
    assert recovered["http_monitor"]["case_id"] is None
    assert recovered["cases"][0]["status"] == "evidence_submitted"
    again = run_checks(3)
    assert len(again["cases"]) == 2
    assert again["http_monitor"]["case_id"] != case_id
    assert client.put(path, headers=admin_headers, json={"enabled": False}).is_success


def test_handoff_shares_fixed_snapshot_with_designated_reviewer(client, admin_headers):
    reviewer = admin_create_user(client, admin_headers, "reviewing-member", "reviewing-member-secret", "viewer")
    outsider = register(client, "outsider-reviewer", "outsider-reviewer-secret")
    case = client.post("/api/support/cases", headers=admin_headers, json={"title": "待交接问题"}).json()
    case_id = case["id"]
    api = f"/api/support/cases/{case_id}"
    assert client.post(api + "/evidence", headers=admin_headers, json={
        "label": "原证据", "text": "ERROR HTTP 502",
    }).is_success
    handoff = f"/api/maintenance/cases/{case_id}/handoff"
    assert client.put(handoff, headers=admin_headers, json={"recipient": "outsider-reviewer", "note": "请核对"}).status_code == 404
    assert client.put(handoff, headers=admin_headers, json={"recipient": "reviewing-member", "note": "请核对"}).is_success
    assert client.post(api + "/evidence", headers=admin_headers, json={
        "label": "交接后的证据", "text": "ERROR HTTP 503",
    }).is_success

    inbox = "/api/maintenance/handoffs"
    item = client.get(inbox, headers=reviewer).json()["items"][0]
    assert [row["label"] for row in item["snapshot"]["evidence"]] == ["原证据"]
    assert client.get(inbox, headers=outsider).json()["items"] == []
    assert client.get(api, headers=reviewer).status_code == 404
    review_url = f"{inbox}/{case_id}/review"
    review = {"version": item["version"], "status": "changes_requested", "note": "请补充操作后的日志"}
    assert client.post(review_url, headers=outsider, json=review).status_code == 404
    assert client.post(review_url, headers=reviewer, json=review).is_success
    assert client.post(review_url, headers=reviewer, json=review).status_code == 409
    assert client.get(api, headers=admin_headers).json()["handoff"]["review_note"] == review["note"]
    assert client.delete(handoff, headers=admin_headers).is_success
    assert client.get(inbox, headers=reviewer).json()["items"] == []

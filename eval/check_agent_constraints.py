"""复核T10的真实证据与当前只读状态，不创建工单、不重启、不调用付费模型。"""
import argparse
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import httpx

from eval.agent_eval import load, save, sha


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=Path("eval/reports/t10_native_20261030T111138"))
    parser.add_argument("--junit", type=Path, default=Path("data/t11_setup/offline_tests.xml"))
    args = parser.parse_args()
    evidence = args.evidence
    source = evidence / "approval_restart"
    summary = load(source / "summary.json")
    approval = load(evidence / "approval_run_1.json")
    rejected = load(evidence / "approval_run_2.json")
    # T10最早的SSE发生在完整输入轨迹补充之前；T11最小补验保留新旧两份证据。
    stream_path = args.output.parent / "current_sse_run.json"
    sse_path = args.output.parent / "current_sse.json"
    stream, sse = load(stream_path), load(sse_path)
    concurrent = load(source / "concurrent_decisions.json")
    passed = {t.attrib["name"] for t in ET.parse(args.junit).findall(".//testcase") if t.find("failure") is None and t.find("error") is None and t.find("skipped") is None}
    guards = {
        "v2-025": "test_tenant_override_and_write_tool_are_never_executed",
        "v2-026": "test_tenant_override_and_write_tool_are_never_executed",
        "v2-027": "test_repeated_call_stops_without_repeating_backend_work",
        "v2-028": "test_timeout_bounds_waiting_and_is_not_a_healthy_result",
        "v2-029": "test_batch_over_budget_has_matching_denial_and_no_fourth_execution",
    }
    assert all(t in passed for t in guards.values()), "Mock约束测试未全部通过"
    checks = {"at": datetime.now(timezone.utc).isoformat(), "mock": [{"id": cid, "test": name, "passed": True} for cid, name in guards.items()],
              "scope": "5个Mock约束+T10审批证据复用+T11最新SSE实测；本命令只读复核，不冒充重新执行重启/并发模型验证",
              "previous_attempt": "旧T10 SSE未保存model_input，首次复核因此失败；未覆盖旧记录，T11新增最新代码的SSE证据补验", "http": []}
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=15) as client:
        def login(name, password):
            r = client.post("/auth/login", json={"username": name, "password": password})
            r.raise_for_status()
            return {"Authorization": "Bearer " + r.json()["access_token"]}

        admin, outsider = login("admin", "admin123"), login("acme_admin", "acme123456")
        for row in (approval, rejected, stream):
            path = f"/api/agent/runs/{row['run_id']}"
            response = client.get(path, headers=admin)
            assert response.status_code == 200
            current = response.json()
            assert current["status"] == row["status"]
            assert any(e["kind"] == "model_input" for e in current["data"]["events"])
            assert any(e["kind"] == "model_call" and e["detail"].get("usage") for e in current["data"]["events"])
            denied = client.get(path, headers=outsider)
            assert denied.status_code == 404
            checks["http"].append({"path": path, "owner": 200, "outsider": 404, "status": current["status"]})
    assert sse and {e["run_id"] for e in sse} == {stream["run_id"]}
    restart_files = (source / "approval_restart.json", source / "rejection_restart.json")
    for path in restart_files:
        data = load(path)
        assert data["exit_code"] == 0 and data["started_after"] != data["started_before"]
    tickets = {r["response"]["ticket_id"] for r in concurrent if r["status"] == 200}
    assert len(concurrent) == 4 and all(r["status"] == 200 for r in concurrent) and tickets == {summary["ticket_id"]}
    assert summary["viewer_blocked"] and summary["cross_tenant_blocked"]
    payload = {"approved": summary["approved_approval"], "rejected": summary["rejected_approval"]}
    probe = """
import json
from sqlalchemy import text
from app.db.base import engine
from app.config import get_settings
with engine.connect() as db:
    counts={key:db.execute(text('SELECT COUNT(*) FROM tickets WHERE approval_id=:aid'),{'aid':aid}).scalar_one() for key,aid in ids.items()}
print(json.dumps({'counts':counts,'mysql':engine.dialect.name,'agent_mode':get_settings().agent_mode}))
"""
    command = [r"D:\Docker\resources\bin\docker.exe", "compose", "exec", "-T", "api", "python", "-"]
    output = subprocess.run(command, input=f"ids={payload!r}\n" + probe, capture_output=True, encoding="utf-8", timeout=30, check=True)
    current = json.loads(output.stdout)
    assert current["counts"] == {"approved": 1, "rejected": 0} and current["mysql"] == "mysql" and current["agent_mode"] == "native"
    checks["real_integration_reused"] = [
        {"id": "v2-030", "passed": True, "checks": "原始SSE同run_id；当前归属租户200/跨租户404；输入与实报usage仍持久化"},
        {"id": "v2-031", "passed": True, "checks": "原始两次API重启时间变化；当前审批run保持approved/rejected"},
        {"id": "v2-032", "passed": True, "checks": "原始4并发同一工单/低权限拒绝；当前实际MySQL批准1条、拒绝0条"},
    ]
    checks["current_backend"] = current
    paths = [args.junit, sse_path, stream_path, evidence / "approval_run_1.json",
             evidence / "approval_run_2.json", source / "summary.json", source / "http.json", source / "concurrent_decisions.json", *restart_files]
    checks["evidence_sha256"] = {str(p).replace("\\", "/"): sha(p) for p in paths}
    save(args.output, checks)
    print("5个Mock约束和3组真实证据只读复核通过；未调用模型。")


if __name__ == "__main__":
    main()

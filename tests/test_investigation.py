import time
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from app.services import log_service
from scripts import metrics_demo


def test_real_request_logs_are_bounded_by_identity_time_and_error():
    before = time.time()
    with TestClient(metrics_demo.app) as client:
        client.post("/control", json={"error_every": 2, "error_code": "ERR_DOWNSTREAM_TIMEOUT"})
        good, failed = client.get("/work"), client.get("/work")
        end = time.time()
        params = {"tenant_id": "default", "service": "order-service", "start": before, "end": end}
        rows = client.get("/logs", params={**params, "error_code": "ERR_DOWNSTREAM_TIMEOUT"}).json()["records"]
        assert len(rows) == 1 and rows[0]["request_id"] == failed.headers["X-Request-ID"]
        assert rows[0]["status_code"] == 500 and good.status_code == 200
        assert rows[0]["fault_injected"] is True
        all_rows = client.get("/logs", params=params).json()["records"]
        assert next(r for r in all_rows if r["request_id"] == good.headers["X-Request-ID"])["fault_injected"] is False
        assert client.get("/logs", params={**params, "tenant_id": "other"}).json()["records"] == []
        assert client.get("/logs", params={**params, "end": before}).json()["records"] == []
        client.post("/control", json={})


def test_log_source_outside_authenticated_scope_is_rejected(monkeypatch):
    real_client = httpx.Client
    records = [{"tenant_id": "other", "service": "order-service", "timestamp": 1000}]
    monkeypatch.setattr(log_service.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"records": records, "matched": 1})), **kwargs
    ))
    result = log_service.query_logs("default", "order-service", 5, evaluation_time=1001)
    assert result["status"] == "unavailable" and result["records"] == []


def test_metrics_and_logs_share_one_investigation_window(monkeypatch):
    from app.agent import tools

    calls = []
    monkeypatch.setattr(tools, "query_service_metrics", lambda **kwargs: calls.append(kwargs) or {
        "summary": "unused", "source": {"kind": "prometheus"}, "service": "order-service",
        "window": {"start": "start", "end": "end"}, "status": "no_data", "anomalies": [],
    })
    monkeypatch.setattr(log_service, "query_logs", lambda tenant_id, **kwargs: calls.append({**kwargs, "tenant_id": tenant_id}) or {"status": "no_data"})
    execute = tools.make_tool_executor(SimpleNamespace(settings=SimpleNamespace(mcp_enabled=False)), "default")
    execute("query_service_metrics", {"service": "order-service", "window_minutes": 5})
    execute("query_logs", {"service": "order-service", "window_minutes": 5})
    assert calls[0]["evaluation_time"] == calls[1]["evaluation_time"]
    assert all(call["tenant_id"] == "default" for call in calls)


def test_four_step_investigation_can_refine_log_query():
    from app.agent.tool_loop import run_tool_loop
    from tests.test_t10_tool_loop import ScriptedAPI, tool

    calls = [tool("query_service_metrics", {"service": "order-service"}, "metrics"),
             tool("query_logs", {"service": "order-service"}, "logs"),
             tool("search_runbook", {"query": "下游超时"}, "sop"),
             tool("query_logs", {"service": "order-service", "error_code": "ERR_DOWNSTREAM_TIMEOUT"}, "refine")]
    api = ScriptedAPI(*({"tool_calls": [call]} for call in calls), {"content": "测试注入的下游超时有日志依据，生产原因尚未确认。"})
    result = run_tool_loop("调查订单服务", api, lambda *_: {"status": "ok"}, max_tool_calls=5)
    assert result["end_reason"] == "answered" and result["tool_calls_executed"] == 4


def test_rule_path_does_not_expose_default_tenant_metrics():
    from app.agent.tools import run_diagnostic_tools
    from app.container import get_container

    with get_container().db_session() as db:
        events, _, _ = run_diagnostic_tools(db, "other", "order-service最近5分钟错误率是多少？")
    result = next(e["result"] for e in events if e["name"] == "query_service_metrics")
    assert result["status"] == "no_data" and result["avg"] == {}


def test_eval_requires_actual_request_evidence_and_matching_windows():
    from eval.investigation_eval import grade_case

    case = {"tools": ["query_logs", "query_service_metrics"], "forbid": [], "window": 5, "code": "ERR_INJECTED"}
    calls = [
        {"name": "query_logs", "executed": True, "args": {"window_minutes": 5}, "result": {
            "status": "ok", "window": {"start": "a", "end": "b"},
            "records": [{"request_id": "actual-request", "error_code": "ERR_INJECTED"}]}},
        {"name": "query_service_metrics", "executed": True, "args": {"window_minutes": 5},
         "result": {"status": "ok", "window": {"start": "a", "end": "b"}, "avg": {"error_rate_pct": 50}}},
    ]
    trace = {"data": {"events": [{"kind": "tool", "detail": call} for call in calls]}}
    answer = {"status": "answered", "answer": "测试环境注入错误"}
    assert all(grade_case(case, answer, trace, {"actual-request"}).values())
    assert not grade_case(case, answer, trace, {"other-request"})["actual_request_evidence"]
    calls[1]["result"]["avg"]["error_rate_pct"] = 0
    assert not grade_case(case, answer, trace, {"actual-request"})["metric_error_observed"]
    calls[1]["result"]["window"]["end"] = "different"
    assert not grade_case(case, answer, trace, {"actual-request"})["aligned_window"]

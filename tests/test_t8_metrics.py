"""T8：Prometheus 协议边界为假端点测试，真实采集另做 Compose 集成。"""
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.services import metrics_service as metrics


def _prometheus(monkeypatch, *, up="1", empty=False, invalid=False, timeout=False):
    settings = Settings(_env_file=None, metrics_provider="prometheus", prometheus_url="http://prom.test")
    monkeypatch.setattr(metrics, "get_settings", lambda: settings)
    calls = []
    real_client = httpx.Client

    def respond(request):
        calls.append(dict(request.url.params))
        if timeout:
            raise httpx.ReadTimeout("演示超时", request=request)
        if invalid:
            return httpx.Response(200, json={"status": "error", "error": "bad query"})
        query = request.url.params["query"]
        value = up if query.startswith("max(up") else "450" if "histogram_quantile" in query else "2.5" if "code=~" in query else "12"
        rows = [] if empty and not query.startswith("max(up") else [{"value": [123, value]}]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": rows}})

    monkeypatch.setattr(metrics.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    return calls


def test_prometheus_values_units_and_one_evaluation_window(monkeypatch):
    calls = _prometheus(monkeypatch)
    result = metrics.query_service_metrics("order-service", 5)
    assert result["status"] == "ok"
    assert result["avg"] == {"qps": 12, "latency_p99_ms": 450, "error_rate_pct": 2.5}
    assert "cpu_pct" not in result["avg"]
    assert result["source"]["kind"] == "prometheus" and result["source"]["environment"] == "test"
    assert result["units"]["qps"] == "requests/s" and len(result["anomalies"]) == 2
    assert len(calls) == 4 and {row["time"] for row in calls} == {str(result["evaluation_time"])}
    assert (datetime.fromisoformat(result["window"]["end"]) - datetime.fromisoformat(result["window"]["start"])).total_seconds() == 300


@pytest.mark.parametrize("options,status", [
    ({"empty": True}, "insufficient_data"),
    ({"up": "0"}, "unavailable"),
    ({"invalid": True}, "unavailable"),
    ({"timeout": True}, "unavailable"),
])
def test_missing_or_failed_metrics_do_not_become_healthy(monkeypatch, options, status):
    _prometheus(monkeypatch, **options)
    result = metrics.query_service_metrics("unknown-service", 5)
    assert result["status"] == status
    assert "证据不足" in result.get("message", "") or "采样不足" in result.get("message", "")
    assert result["anomalies"] == []
    assert result["source"]["kind"] == "prometheus"


def test_service_label_cannot_inject_promql(monkeypatch):
    calls = _prometheus(monkeypatch)
    result = metrics.query_service_metrics('order-service"} or up{job="x')
    assert result["status"] == "invalid_service" and not calls


def test_no_matching_fault_returns_empty_and_keeps_tenant_isolation():
    from app.agent.tools import query_fault_history
    from app.db.base import SessionLocal
    from app.db.models import FaultRecord

    with SessionLocal() as db:
        for tenant in ("default", "other-test-tenant"):
            db.add(FaultRecord(tenant_id=tenant, service="t8-service", error_code="ERR_T8", title="历史参考", symptom="演示", root_cause="演示", resolution="演示", severity="P2"))
        db.commit()
        assert query_fault_history(db, "default", service="absent-t8-service") == []
        rows = query_fault_history(db, "default", service="t8-service", error_code="ERR_T8")
        assert len(rows) == 1 and rows[0]["match_type"] == "exact"


def test_metric_summary_preserves_operational_thresholds(monkeypatch):
    from app.agent import tools
    from app.db.base import SessionLocal

    _prometheus(monkeypatch)
    with SessionLocal() as db:
        events, _, _ = tools.run_diagnostic_tools(db, "default", "order-service 延迟异常")
    event = next(e for e in events if e["name"] == "query_service_metrics")
    assert all(anomaly in event["summary"] for anomaly in event["anomalies"])


def test_demo_measures_real_requests_and_controls_injected_errors():
    from scripts.metrics_demo import app

    with TestClient(app) as client:
        assert client.post("/control", json={"delay_ms": 0, "error_every": 2}).status_code == 200
        assert client.get("/work").status_code == 200
        assert client.get("/work").status_code == 500
        text = client.get("/metrics").text
        assert 'aiops_demo_requests_total{code="500",service="order-service"}' in text
        assert "aiops_demo_request_duration_seconds_bucket" in text
        assert client.post("/control", json={"delay_ms": -1}).status_code == 422

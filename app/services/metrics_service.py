"""指标查询：正式 Compose 查询 Prometheus，离线 Mock 明确标记来源。"""
import hashlib
import math
import re
import time
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.services.run_service import tool_time_left


def _seeded_uniform(seed: str, index: int) -> float:
    digest = hashlib.md5(f"{seed}:{index}".encode()).digest()
    return int.from_bytes(digest[:4], "little") / 0xFFFFFFFF


# 预置异常服务：评测/演示用，使工具能发现异常指标
ANOMALY_SERVICES = {"order-service", "payment-service"}


def _mock_metrics(service: str, window_minutes: int) -> dict:
    service = service.strip().lower()
    points = min(max(window_minutes, 5), 240)
    base_qps = 100 + int(_seeded_uniform(service, 1) * 400)
    base_p99 = 40 + _seeded_uniform(service, 2) * 120
    base_err = _seeded_uniform(service, 3) * 0.5
    base_cpu = 20 + _seeded_uniform(service, 4) * 40

    anomaly = service in ANOMALY_SERVICES
    if anomaly:
        base_p99 += 500
        base_err += 4.0
        base_cpu += 35

    series = {
        "qps": [round(base_qps * (0.8 + 0.4 * _seeded_uniform(f"{service}-qps", i)), 1) for i in range(points)],
        "latency_p99_ms": [round(base_p99 * (0.85 + 0.3 * _seeded_uniform(f"{service}-p99", i)), 1) for i in range(points)],
        "error_rate_pct": [round(max(base_err * (0.8 + 0.5 * _seeded_uniform(f"{service}-err", i)), 0), 2) for i in range(points)],
        "cpu_pct": [round(min(base_cpu * (0.9 + 0.2 * _seeded_uniform(f"{service}-cpu", i)), 100), 1) for i in range(points)],
    }
    avg = {k: round(sum(v) / len(v), 2) for k, v in series.items()}
    anomalies = []
    if avg["latency_p99_ms"] > 300:
        anomalies.append(f"P99 延迟 {avg['latency_p99_ms']}ms 超过 300ms 阈值")
    if avg["error_rate_pct"] > 1.0:
        anomalies.append(f"错误率 {avg['error_rate_pct']}% 超过 1% 阈值")
    if avg["cpu_pct"] > 85:
        anomalies.append(f"CPU 使用率 {avg['cpu_pct']}% 超过 85% 阈值")
    return {
        "service": service,
        "window_minutes": window_minutes,
        "avg": avg,
        "series_sample": {k: v[-6:] for k, v in series.items()},
        "anomalies": anomalies,
    }


def query_service_metrics(service: str, window_minutes: int = 60) -> dict:
    settings = get_settings()
    service = service.strip().lower()
    window_minutes = min(max(window_minutes, 5), 240)
    end = round(time.time(), 3)
    result = {
        "service": service,
        "window_minutes": window_minutes,
        "evaluation_time": end,
        "window": {
            "start": datetime.fromtimestamp(end - window_minutes * 60, timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(end, timezone.utc).isoformat(),
        },
        "units": {"qps": "requests/s", "latency_p99_ms": "ms", "error_rate_pct": "%"},
        "source": {"kind": settings.metrics_provider, "environment": "test", "faults": "测试注入"},
        "status": "insufficient_data",
        "avg": {},
        "anomalies": [],
    }
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", service):
        return {**result, "status": "invalid_service", "message": "服务标识无效，指标证据不足"}
    if settings.metrics_provider == "mock":
        return {**result, **_mock_metrics(service, window_minutes), "status": "ok"}
    if settings.metrics_provider != "prometheus":
        raise ValueError("metrics_provider 必须是 mock 或 prometheus")

    result["source"]["url"] = settings.prometheus_url
    selector = f'service="{service}"'
    window = f"{window_minutes * 60}s"
    requests = f"aiops_demo_requests_total{{{selector}}}"
    queries = {
        "scrape_up": 'max(up{job="opspilot-demo"})',
        "qps": f"sum(rate({requests}[{window}]))",
        "latency_p99_ms": (
            f"1000 * histogram_quantile(0.99, sum by (le) "
            f"(rate(aiops_demo_request_duration_seconds_bucket{{{selector}}}[{window}])))"
        ),
        "error_rate_pct": (
            f'100 * sum(rate(aiops_demo_requests_total{{{selector},code=~"5.."}}[{window}]))'
            f" / sum(rate({requests}[{window}]))"
        ),
    }
    result["queries"] = queries
    values = {}
    try:
        with httpx.Client(timeout=settings.prometheus_timeout_seconds) as client:
            for name, query in queries.items():
                remaining = tool_time_left()
                response = client.get(
                    settings.prometheus_url.rstrip("/") + "/api/v1/query",
                    params={"query": query, "time": str(end)},
                    timeout=min(settings.prometheus_timeout_seconds, remaining) if remaining is not None else settings.prometheus_timeout_seconds,
                )
                response.raise_for_status()
                body = response.json()
                if body["status"] != "success":
                    raise ValueError("Prometheus 查询失败")
                rows = body["data"]["result"]
                value = float(rows[0]["value"][1]) if rows else None
                values[name] = value if value is not None and math.isfinite(value) else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
        return {**result, "status": "unavailable", "message": "Prometheus 不可用，指标证据不足", "error_type": type(exc).__name__}

    result["avg"] = {name: values[name] for name in result["units"]}
    if values["scrape_up"] != 1:
        return {**result, "status": "unavailable", "message": "测试服务采集目标不可用，指标证据不足"}
    if any(value is None for value in result["avg"].values()):
        return {**result, "message": "未找到服务指标或采样不足，不能据此判定服务正常或根因"}
    result["status"] = "ok"
    if values["latency_p99_ms"] > 300:
        result["anomalies"].append(f"P99 延迟 {values['latency_p99_ms']:.2f}ms 超过 300ms 阈值")
    if values["error_rate_pct"] > 1:
        result["anomalies"].append(f"错误率 {values['error_rate_pct']:.2f}% 超过 1% 阈值")
    return result

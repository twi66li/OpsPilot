"""指标查询服务：模拟 Prometheus 查询接口，为 Agent 提供实时诊断工具。

确定性伪随机生成（以 service+window 为种子），保证演示/评测可复现。
"""
import hashlib


def _seeded_uniform(seed: str, index: int) -> float:
    digest = hashlib.md5(f"{seed}:{index}".encode()).digest()
    return int.from_bytes(digest[:4], "little") / 0xFFFFFFFF


# 预置异常服务：评测/演示用，使工具能发现异常指标
ANOMALY_SERVICES = {"order-service", "payment-service"}


def query_service_metrics(service: str, window_minutes: int = 60) -> dict:
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

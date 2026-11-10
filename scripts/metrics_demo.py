"""仅供本地验收的测试服务：注入延迟/错误，Prometheus 采集实际请求。"""
import asyncio
import time
import uuid
from collections import deque
from typing import Literal

from fastapi import FastAPI, Query, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

app = FastAPI(title="OpsPilot 测试服务（非生产监控）")
registry = CollectorRegistry()
requests = Counter("aiops_demo_requests", "测试请求数", ["service", "code"], registry=registry)
latency = Histogram(
    "aiops_demo_request_duration_seconds", "实际请求耗时", ["service"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5), registry=registry,
)
for code in ("200", "500"):
    requests.labels(service="order-service", code=code)
latency.labels(service="order-service")


class FaultConfig(BaseModel):
    delay_ms: int = Field(default=0, ge=0, le=3000)
    error_every: int = Field(default=0, ge=0, le=1000)
    error_code: Literal["ERR_INJECTED", "ERR_DOWNSTREAM_TIMEOUT", "ERR_DB_POOL_TIMEOUT"] = "ERR_INJECTED"


fault = FaultConfig()
sequence = 0
request_logs: deque = deque(maxlen=2000)


@app.post("/control")
async def control(config: FaultConfig):
    global fault, sequence
    fault, sequence = config, 0
    return {"service": "order-service", "environment": "test", "fault_injection": config.model_dump()}


@app.get("/work")
async def work():
    global sequence
    sequence += 1
    current, config = sequence, fault
    start = time.perf_counter()
    await asyncio.sleep(config.delay_ms / 1000)
    status = 500 if config.error_every and current % config.error_every == 0 else 200
    elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
    request_id = uuid.uuid4().hex
    request_logs.append({
        "timestamp": time.time(), "tenant_id": "default", "service": "order-service", "environment": "test",
        "request_id": request_id, "status_code": status, "elapsed_ms": elapsed_ms,
        "error_code": config.error_code if status == 500 else "", "fault_injected": bool(config.delay_ms or status == 500),
        "message": f"测试注入：{config.error_code}" if status == 500 else "请求完成",
    })
    requests.labels(service="order-service", code=str(status)).inc()
    latency.labels(service="order-service").observe(time.perf_counter() - start)
    return Response(status_code=status, content="injected error" if status == 500 else "ok", headers={"X-Request-ID": request_id})


@app.get("/logs")
async def logs(tenant_id: str, service: str, start: float, end: float, limit: int = Query(default=30, ge=1, le=100), error_code: str | None = None):
    rows = [row for row in list(request_logs) if row["tenant_id"] == tenant_id and row["service"] == service
            and start <= row["timestamp"] <= end and (not error_code or row["error_code"] == error_code)]
    return {"records": rows[-limit:], "matched": len(rows), "source": "actual_test_requests"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

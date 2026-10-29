"""Agent 工具集：实体抽取、指标查询、故障史查询。

TOOL_REGISTRY 供 /api/agent/tools 展示与 Langfuse 打点对齐。
"""
import re
import time

from sqlalchemy.orm import Session

from app.db.models import FaultRecord
from app.services.metrics_service import query_service_metrics

# 中文服务名 → 服务标识（演示环境约定）
SERVICE_ALIAS = {
    "订单服务": "order-service",
    "支付服务": "payment-service",
    "用户服务": "user-service",
    "库存服务": "inventory-service",
    "网关": "api-gateway-service",
    "通知服务": "notify-service",
}

ERROR_CODE_RE = re.compile(r"(?:ERR|ERROR)[_-]?[A-Za-z0-9]+", re.IGNORECASE)
SERVICE_RE = re.compile(r"[a-z][a-z0-9]*-service", re.IGNORECASE)


def extract_entities(question: str) -> dict:
    """从自然语言问题中抽取错误码与服务名（工具路由依据）。"""
    error_codes = sorted({m.group(0).upper().replace("-", "_") for m in ERROR_CODE_RE.finditer(question)})
    services = sorted({m.group(0).lower() for m in SERVICE_RE.finditer(question)})
    for zh, en in SERVICE_ALIAS.items():
        if zh in question and en not in services:
            services.append(en)
    return {"error_codes": error_codes, "services": services}


def query_fault_history(db: Session, tenant_id: str, service: str = "", error_code: str = "", limit: int = 5) -> list[dict]:
    query = db.query(FaultRecord).filter(FaultRecord.tenant_id == tenant_id)
    if service:
        query = query.filter(FaultRecord.service == service.lower())
    if error_code:
        query = query.filter(FaultRecord.error_code == error_code.upper())
    rows = query.order_by(FaultRecord.occurred_at.desc()).limit(limit).all()
    # 无精确匹配时退化为租户内最近记录，保证工具总能提供参考
    if not rows:
        rows = db.query(FaultRecord).filter(FaultRecord.tenant_id == tenant_id).order_by(FaultRecord.occurred_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "service": r.service,
            "error_code": r.error_code,
            "title": r.title,
            "symptom": r.symptom[:200],
            "root_cause": r.root_cause[:200],
            "resolution": r.resolution[:200],
            "severity": r.severity,
            "occurred_at": r.occurred_at.isoformat(),
        }
        for r in rows
    ]


def run_diagnostic_tools(db: Session, tenant_id: str, question: str) -> tuple[list[dict], list[dict], dict]:
    """执行工具链：返回 (tool_events, fault_records, metrics)。

    tool_events 记录每次调用的名称/参数/耗时/关键发现，随 SSE 推送并写入 Langfuse。
    """
    entities = extract_entities(question)
    tool_events: list[dict] = []
    metrics_all: dict = {}
    fault_records: list[dict] = []

    for service in entities["services"][:2]:
        start = time.perf_counter()
        result = query_service_metrics(service)
        metrics_all[service] = result
        tool_events.append(
            {
                "name": "query_service_metrics",
                "args": {"service": service, "window_minutes": 60},
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "summary": f"QPS={result['avg']['qps']}, P99={result['avg']['latency_p99_ms']}ms, 错误率={result['avg']['error_rate_pct']}%",
                "anomalies": result["anomalies"],
            }
        )

    start = time.perf_counter()
    fault_records = query_fault_history(db, tenant_id, service=entities["services"][0] if entities["services"] else "", error_code=entities["error_codes"][0] if entities["error_codes"] else "")
    tool_events.append(
        {
            "name": "query_fault_history",
            "args": {"service": entities["services"][:1], "error_codes": entities["error_codes"][:1]},
            "duration_ms": round((time.perf_counter() - start) * 1000, 2),
            "summary": f"命中 {len(fault_records)} 条历史故障",
        }
    )
    return tool_events, fault_records, metrics_all


TOOL_REGISTRY = [
    {"name": "query_service_metrics", "description": "查询服务近 1 小时的 QPS / P99 延迟 / 错误率 / CPU 指标，返回异常项"},
    {"name": "query_fault_history", "description": "按服务名 / 错误码检索历史故障记录（根因与解决方案）"},
]

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
# 否定引导词：其后的从句内容（如"不要拿 X 替代"）不是用户想查询的实体；
# "是不是/看不看"中的"不是/不看"不是否定引导，用负向后顾排除
NEGATION_CUE_RE = re.compile(r"(?:不要|不用|不得|不必|无需|禁止|(?<!是)不是|不查|(?<!看)不看|别拿|别用|别提)")
_CLAUSE_BOUNDARY_RE = re.compile(r"[？?！!。；;，,、]")
TIME_WINDOW_RE = re.compile(r"(?:近|最近)\s*(\d+)\s*(小时|h|分钟|m|min)?", re.IGNORECASE)
# 出现这些状态/指标词且抽取到服务名时，规则链才查询实时指标
METRICS_HINT_RE = re.compile(
    r"(?:QPS|P99|错误率|延迟|超时|异常|错误|故障|告警|正常|状态|指标|监控|CPU|内存|流量|吞吐|慢|(?<![0-9A-Za-z])5\d{2}(?![0-9]))",
    re.IGNORECASE,
)


def strip_negations(question: str) -> str:
    """去掉否定引导从句（从否定词到下一个分句边界），避免其内容被当作查询实体/触发词。"""
    out, pos = [], 0
    for match in NEGATION_CUE_RE.finditer(question):
        if match.start() < pos:
            continue
        out.append(question[pos:match.start()])
        boundary = _CLAUSE_BOUNDARY_RE.search(question, match.end())
        pos = boundary.end() if boundary else len(question)
    out.append(question[pos:])
    return "".join(out)


def extract_window_minutes(question: str) -> int:
    """抽取"近/最近 N 分钟/小时"为 window_minutes；无时间词默认 60，边界与 MetricsArgs(5–240) 一致。"""
    match = TIME_WINDOW_RE.search(question)
    if not match:
        return 60
    value = int(match.group(1))
    if match.group(2) and match.group(2).lower().startswith(("h", "小")):
        value *= 60
    return min(max(value, 5), 240)


def extract_entities(question: str) -> dict:
    """从自然语言问题中抽取错误码与服务名（工具路由依据）；否定从句先剥离。"""
    text = strip_negations(question)
    error_codes = sorted({m.group(0).upper().replace("-", "_") for m in ERROR_CODE_RE.finditer(text)})
    services = sorted({m.group(0).lower() for m in SERVICE_RE.finditer(text)})
    for zh, en in SERVICE_ALIAS.items():
        if zh in text and en not in services:
            services.append(en)
    return {"error_codes": error_codes, "services": services}


def query_fault_history(db: Session, tenant_id: str, service: str = "", error_code: str = "", limit: int = 5) -> list[dict]:
    query = db.query(FaultRecord).filter(FaultRecord.tenant_id == tenant_id)
    if service:
        query = query.filter(FaultRecord.service == service.lower())
    if error_code:
        query = query.filter(FaultRecord.error_code == error_code.upper())
    rows = query.order_by(FaultRecord.occurred_at.desc()).limit(limit).all()
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
            "match_type": "exact" if service or error_code else "tenant_recent",
        }
        for r in rows
    ]


def metrics_summary(result):
    source = "Prometheus测试服务实际采集（故障为测试注入）" if result["source"]["kind"] == "prometheus" else "Mock模拟数据"
    if result["source"].get("replay"):
        source += "（录制证据回放）"
    evidence = f"{result['service']}；来源：{source}；时间窗：{result['window']['start']} 至 {result['window']['end']}"
    if result["status"] == "ok":
        evidence += f"；QPS={result['avg']['qps']} requests/s, P99={result['avg']['latency_p99_ms']}ms, 错误率={result['avg']['error_rate_pct']}%"
    else:
        evidence += f"；状态={result['status']}，{result.get('message', '指标证据不足')}"
    if result["anomalies"]:
        evidence += "；本次阈值判定：" + "；".join(result["anomalies"])
    return evidence


def make_tool_executor(container, tenant_id):
    """tenant_id 来自服务端认证上下文，模型参数无法覆盖它。"""
    citation_ids = {}

    def execute(name, args):
        if name == "search_runbook":
            with container.db_session() as db:
                result = container.retriever.retrieve(db, tenant_id, args["query"], mode=container.settings.retrieval_mode, top_k=args["top_k"])
            hits = []
            for hit in result.hits:
                data = hit.to_dict()
                citation = citation_ids.setdefault(data["chunk_id"], len(citation_ids) + 1)
                hits.append({**data, "text": data["text"][:500], "citation_id": citation})
            return {"status": "ok" if hits else "no_data", "hits": hits, "summary": f"检索到{len(hits)}个手册片段，片段不代表当前实时状态"}
        if name == "query_service_metrics":
            result = query_service_metrics(**args)
            return {**result, "summary": metrics_summary(result)}
        if name == "query_fault_history":
            with container.db_session() as db:
                rows = query_fault_history(db, tenant_id, **args)
            return {"status": "ok" if rows else "no_data", "records": rows, "summary": f"历史精确匹配{len(rows)}条，仅供参考，不能确认本次根因"}
        raise ValueError("只允许三个只读工具")

    return execute


def run_diagnostic_tools(db: Session, tenant_id: str, question: str) -> tuple[list[dict], list[dict], dict]:
    """执行工具链：返回 (tool_events, fault_records, metrics)。

    tool_events 记录每次调用的名称/参数/耗时/关键发现，随 SSE 推送并写入 Langfuse。
    意图门控：仅当抽取到服务名且问题含状态/指标词才查实时指标；历史故障仅在有
    服务名或错误码过滤时查询，避免无关问题触发无关工具。
    """
    entities = extract_entities(question)
    text = strip_negations(question)
    window_minutes = extract_window_minutes(text)
    tool_events: list[dict] = []
    metrics_all: dict = {}
    fault_records: list[dict] = []

    if entities["services"] and METRICS_HINT_RE.search(text):
        for service in entities["services"][:2]:
            start = time.perf_counter()
            result = query_service_metrics(service, window_minutes)
            metrics_all[service] = result
            evidence = metrics_summary(result)
            tool_events.append(
                {
                    "name": "query_service_metrics",
                    "args": {"service": service, "window_minutes": window_minutes},
                    "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                    "summary": evidence,
                    "anomalies": result["anomalies"],
                    "result": result,
                }
            )

    if entities["services"] or entities["error_codes"]:
        start = time.perf_counter()
        fault_records = query_fault_history(db, tenant_id, service=entities["services"][0] if entities["services"] else "", error_code=entities["error_codes"][0] if entities["error_codes"] else "")
        tool_events.append(
            {
                "name": "query_fault_history",
                "args": {"service": entities["services"][:1], "error_codes": entities["error_codes"][:1]},
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "summary": (
                    f"命中 {len(fault_records)} 条精确匹配历史故障，仅供排查参考"
                    if fault_records and (entities["services"] or entities["error_codes"])
                    else "未找到匹配历史故障，不能据此确认根因"
                ),
            }
        )
    return tool_events, fault_records, metrics_all


TOOL_REGISTRY = [
    {"name": "search_runbook", "description": "只读检索本租户的运维手册和SOP，返回依据片段"},
    {"name": "query_service_metrics", "description": "只读查询指定测试服务最近 window_minutes 分钟（5–240，以当前时间为终点）的 QPS / P99 延迟 / 错误率，返回来源、时间窗和证据状态"},
    {"name": "query_fault_history", "description": "按服务名 / 错误码检索历史故障记录（根因与解决方案）"},
]

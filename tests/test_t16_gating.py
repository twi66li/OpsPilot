"""T16：三类明确问题的最小修复回归——无关工具查询门控、服务名清单、时间窗参数。"""
import json
from types import SimpleNamespace

import httpx
import pytest

from app.agent.llm import detect_intent
from app.agent.tool_loop import DESCRIPTIONS, SYSTEM, run_tool_loop
from app.agent.tools import (
    TOOL_REGISTRY,
    extract_entities,
    extract_window_minutes,
    make_tool_executor,
    run_diagnostic_tools,
    strip_negations,
)
from app.config import Settings
from app.services import metrics_service as metrics
from app.services.metrics_service import KNOWN_SERVICES

# ---- 否定剥离与实体抽取 ----

def test_negation_clause_is_stripped_from_entity_extraction():
    assert extract_entities("ghost-service 异常，不要拿 order-service 替代")["services"] == ["ghost-service"]
    assert extract_entities("order-service 延迟，不用查 payment-service")["services"] == ["order-service"]
    assert extract_entities("网关发布期间 502")["services"] == ["api-gateway-service"]
    assert extract_entities("ERR_7010 与 ERR_7011 比较")["error_codes"] == ["ERR_7010", "ERR_7011"]


def test_interrogative_is_not_treated_as_negation():
    text = strip_negations("看看现在是不是正常，当前 P99 是多少？")
    assert "正常" in text and "P99" in text


# ---- 时间窗抽取 ----

@pytest.mark.parametrize("question,expected", [
    ("order-service 近5分钟错误率是多少", 5),
    ("最近3小时的P99如何", 180),
    ("近2h 的吞吐", 120),
    ("无任何时间表达的问题", 60),
    ("近1000分钟指标", 240),
])
def test_window_minutes_extraction(question, expected):
    assert extract_window_minutes(question) == expected


# ---- 规则链意图门控 ----

def test_metrics_require_service_and_status_hint():
    from app.db.base import SessionLocal

    with SessionLocal() as db:
        events, _, _ = run_diagnostic_tools(db, "default", "仅根据手册说明 order-service 的接口规范")
    assert [e["name"] for e in events] == ["query_fault_history"]


def test_metrics_run_for_service_with_hint_and_default_window():
    from app.db.base import SessionLocal

    with SessionLocal() as db:
        events, _, _ = run_diagnostic_tools(db, "default", "order-service 接口大量超时，如何排查？")
    metrics_events = [e for e in events if e["name"] == "query_service_metrics"]
    assert metrics_events and metrics_events[0]["args"]["window_minutes"] == 60
    assert any(e["name"] == "query_fault_history" for e in events)


def test_rule_chain_passes_extracted_window():
    from app.db.base import SessionLocal

    with SessionLocal() as db:
        events, _, _ = run_diagnostic_tools(db, "default", "order-service 近5分钟错误率是多少？")
    metrics_events = [e for e in events if e["name"] == "query_service_metrics"]
    assert metrics_events and metrics_events[0]["args"]["window_minutes"] == 5


def test_no_entities_means_no_rule_tools():
    from app.db.base import SessionLocal

    with SessionLocal() as db:
        events, fault_records, metrics_all = run_diagnostic_tools(db, "default", "今天午饭吃什么比较好？")
    assert events == [] and fault_records == [] and metrics_all == {}


def test_error_code_only_still_queries_history():
    from app.db.base import SessionLocal

    with SessionLocal() as db:
        events, _, _ = run_diagnostic_tools(db, "default", "ERR_5003 数据库连接池耗尽应该怎么处理？")
    assert [e["name"] for e in events] == ["query_fault_history"]
    assert events[0]["args"]["error_codes"] == ["ERR_5003"]


# ---- 服务名清单校验 ----

def test_mock_unknown_service_returns_unknown_service():
    result = metrics.query_service_metrics("db-service")
    assert result["status"] == "unknown_service"
    assert "order-service" in result["valid_services"] and "api-gateway-service" in result["valid_services"]
    assert result["avg"] == {} and result["anomalies"] == []


def test_mock_known_service_still_synthesizes_ok():
    result = metrics.query_service_metrics("order-service")
    assert result["status"] == "ok" and result["avg"]


def test_known_services_covers_demo_inventory():
    assert KNOWN_SERVICES == frozenset(
        {"order-service", "payment-service", "user-service", "inventory-service", "api-gateway-service", "notify-service"}
    )


def _prometheus(monkeypatch, *, up="1", empty=False):
    settings = Settings(_env_file=None, metrics_provider="prometheus", prometheus_url="http://prom.test")
    monkeypatch.setattr(metrics, "get_settings", lambda: settings)
    real_client = httpx.Client

    def respond(request):
        query = request.url.params["query"]
        value = up if query.startswith("max(up") else "450" if "histogram_quantile" in query else "2.5" if "code=~" in query else "12"
        rows = [] if empty and not query.startswith("max(up") else [{"value": [123, value]}]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": rows}})

    monkeypatch.setattr(metrics.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))


def test_prometheus_no_series_message_distinguishes_service_name(monkeypatch):
    _prometheus(monkeypatch, empty=True)
    result = metrics.query_service_metrics("order-service", 5)
    assert result["status"] == "insufficient_data"
    assert "服务名可能不存在" in result["message"] and "采样不足" in result["message"]


# ---- 原生循环提示词与未知服务回传 ----

def test_native_system_prompt_carries_constraints():
    assert "不调用工具" in SYSTEM
    assert "order-service、payment-service" in SYSTEM
    assert "5–240" in SYSTEM and "UTC" in SYSTEM and "北京时间" in SYSTEM
    assert "5–240" in DESCRIPTIONS["query_service_metrics"]


def test_tool_registry_description_matches_parameter():
    registry = {t["name"]: t["description"] for t in TOOL_REGISTRY}
    assert "window_minutes" in registry["query_service_metrics"] and "5–240" in registry["query_service_metrics"]


class _ScriptedAPI:
    def __init__(self, *messages):
        self.messages = list(messages)

    def tool_turn(self, messages, tools, *, allow_tools=True):
        return {"role": "assistant", **self.messages.pop(0)}


class _MetricsOnlyContainer:
    settings = SimpleNamespace(retrieval_mode="bm25")

    def db_session(self):
        raise AssertionError("本用例只允许 metrics 工具，不应打开数据库会话")


def _tool(name, args, key="call-1"):
    return {"id": key, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_native_loop_receives_unknown_service_for_made_up_name():
    api = _ScriptedAPI(
        {"tool_calls": [_tool("query_service_metrics", {"service": "gateway"})]},
        {"content": "gateway 不在已知服务清单，请确认服务名。"},
    )
    result = run_tool_loop("网关当前P99是多少", api, make_tool_executor(_MetricsOnlyContainer(), "default"))
    assert result["end_reason"] == "answered" and result["tool_calls_executed"] == 1
    tool_message = next(m for m in result["messages"] if m["role"] == "tool")
    payload = json.loads(tool_message["content"])
    assert payload["status"] == "unknown_service"
    assert "api-gateway-service" in payload["valid_services"]


# ---- 建单意图仅由祈使短语触发 ----

@pytest.mark.parametrize("question,intent", [
    ("帮我建个工单", "create_ticket"),
    ("请创建工单", "create_ticket"),
    ("订单服务又超时了，请帮我创建工单跟进处理。", "create_ticket"),
    ("我要提单", "create_ticket"),
    ("建单", "create_ticket"),
    ("什么情况该建工单", "diagnose"),
    ("不要建单", "diagnose"),
    ("工单流程是什么", "diagnose"),
    ("查询order-service指标，不要创建工单", "diagnose"),
    ("帮我看下这个ticket该不该关", "diagnose"),
])
def test_detect_intent_requires_imperative_request(question, intent):
    assert detect_intent(question) == intent

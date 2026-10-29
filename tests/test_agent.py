"""诊断工作流测试：LangGraph 全链路（检索 → 判断 → 工具 → 回答）。"""
from tests.conftest import new_session


def _chat(client, headers, session_id: str, message: str) -> dict:
    resp = client.post("/api/agent/chat", json={"session_id": session_id, "message": message}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_diagnose_flow_with_error_code(client, admin_headers):
    session_id = new_session(client, admin_headers)
    result = _chat(client, admin_headers, session_id, "ERR_5003 数据库连接池耗尽应该怎么处理？")

    assert result["status"] == "answered"
    assert result["intent"] == "diagnose"
    assert "连接池" in result["answer"]
    assert result["citations"], "应包含引用文档"

    event_types = [e["type"] for e in result["events"]]
    node_names = [e["node"] for e in result["events"] if e["type"] == "node_status"]
    for node in ("route_intent", "retrieve", "grade", "call_tools", "generate"):
        assert node in node_names, f"缺少节点事件: {node}"
    assert "retrieval" in event_types
    assert "tool" in event_types  # 故障史工具
    retrieval = next(e for e in result["events"] if e["type"] == "retrieval")
    assert retrieval["mode"] in ("dense", "bm25", "hybrid")
    assert retrieval["hits"]


def test_tool_metrics_called_for_service_question(client, admin_headers):
    session_id = new_session(client, admin_headers)
    result = _chat(client, admin_headers, session_id, "order-service 接口大量超时，如何排查？")
    tool_events = [e for e in result["events"] if e["type"] == "tool"]
    tool_names = [e["name"] for e in tool_events]
    assert "query_service_metrics" in tool_names
    assert "query_fault_history" in tool_names
    metrics_event = next(e for e in tool_events if e["name"] == "query_service_metrics")
    assert metrics_event["anomalies"], "order-service 为预置异常服务，应报告指标异常"


def test_multi_turn_state_reset(client, admin_headers):
    session_id = new_session(client, admin_headers)
    first = _chat(client, admin_headers, session_id, "Kafka 消息消费积压怎么处理？")
    assert first["status"] == "answered"
    second = _chat(client, admin_headers, session_id, "ERR_5003 连接池又耗尽了，如何应急处理？")
    assert second["status"] == "answered"
    assert "连接池" in second["answer"]


def test_irrelevant_question_gets_refusal(client, admin_headers):
    session_id = new_session(client, admin_headers)
    result = _chat(client, admin_headers, session_id, "今天午饭吃什么比较好？")
    assert result["status"] == "answered"
    assert "未找到" in result["answer"] or "无法给出" in result["answer"]


def test_insufficient_triggers_reretrieve(client, admin_headers):
    """构造首轮检索不足的用例：改写后补充检索应发生（iteration=2）。"""
    session_id = new_session(client, admin_headers)
    result = _chat(client, admin_headers, session_id, "请介绍一下上季度全公司的组织架构调整方案")
    grades = [e for e in result["events"] if e["type"] == "grade"]
    rewrites = [e for e in result["events"] if e["type"] == "rewrite"]
    assert grades and rewrites, "检索不足时应出现 grade 与 rewrite 节点"


def test_tools_endpoint(client, admin_headers):
    resp = client.get("/api/agent/tools", headers=admin_headers)
    names = [t["name"] for t in resp.json()]
    assert "query_service_metrics" in names and "query_fault_history" in names

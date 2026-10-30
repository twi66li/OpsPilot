"""原生协议形状的脚本模型，仅验证程序约束；真实模型能力另有API探针。"""
import copy
import json
import time

from app.agent.tool_loop import run_tool_loop
from tests.conftest import new_session, register


def tool(name, args, key="call-1"):
    return {"id": key, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedAPI:
    def __init__(self, *messages):
        self.messages = list(messages)
        self.requests = []

    def tool_turn(self, messages, tools, *, allow_tools=True):
        self.requests.append({"messages": copy.deepcopy(messages), "allow_tools": allow_tools})
        return {"role": "assistant", **self.messages.pop(0)}


def test_real_protocol_ids_and_tool_result_are_passed_back():
    api = ScriptedAPI({"tool_calls": [tool("search_runbook", {"query": "连接池"})]}, {"content": "依据手册处理。"})
    executed = []
    result = run_tool_loop("连接池怎么排查", api, lambda name, args: executed.append((name, args)) or {"status": "ok", "hits": [{"text": "检查慢SQL"}]})
    assert result["end_reason"] == "answered" and result["tool_calls_executed"] == 1
    assert executed == [("search_runbook", {"query": "连接池", "top_k": 3})]
    last = api.requests[-1]["messages"][-1]
    assert last["role"] == "tool" and last["tool_call_id"] == "call-1"
    assert "检查慢SQL" in last["content"]


def test_tenant_override_and_write_tool_are_never_executed():
    api = ScriptedAPI({"tool_calls": [tool("query_service_metrics", {"service": "order-service", "tenant_id": "other"}), tool("create_ticket", {}, "write")]}, {"content": "参数无效，未执行。"})
    result = run_tool_loop("越权", api, lambda *_: (_ for _ in ()).throw(AssertionError("不得执行")))
    assert result["tool_calls_executed"] == 0
    assert all(e["result"]["status"] == "invalid_arguments" for e in result["tool_events"])


def test_repeated_call_stops_without_repeating_backend_work():
    call = tool("query_service_metrics", {"service": "order-service"})
    api = ScriptedAPI({"tool_calls": [call]}, {"tool_calls": [{**call, "id": "repeat"}]})
    result = run_tool_loop("查询指标", api, lambda *_: {"status": "ok", "qps": 2})
    assert result["end_reason"] == "repeated_no_progress"
    assert result["tool_calls_used"] == 2 and result["tool_calls_executed"] == 1


def test_batch_over_budget_has_matching_denial_and_no_fourth_execution():
    calls = [tool("query_service_metrics", {"service": f"demo-{i}"}, f"c{i}") for i in range(4)]
    api = ScriptedAPI({"tool_calls": calls}, {"content": "仅依据已有证据。"})
    seen = []
    result = run_tool_loop("查询", api, lambda name, args: seen.append(args) or {"status": "ok"})
    assert len(seen) == result["tool_calls_used"] == 3
    assert api.requests[-1]["allow_tools"] is False
    replies = [m for m in result["messages"] if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in replies} == {f"c{i}" for i in range(4)}
    assert json.loads(replies[-1]["content"])["status"] == "budget_exhausted"
    assert "3次上限" in result["answer"]


def test_timeout_bounds_waiting_and_is_not_a_healthy_result():
    def slow(*_):
        time.sleep(0.15)
        return {"status": "ok"}

    started = time.perf_counter()
    result = run_tool_loop("查询", ScriptedAPI({"tool_calls": [tool("query_service_metrics", {"service": "order-service"})]}), slow, timeout=0.01)
    assert time.perf_counter() - started < 0.12
    assert result["end_reason"] == "tool_timeout" and result["usage_may_arrive_later"]
    assert result["tool_events"][0]["result"]["status"] == "timeout"


def test_backend_value_error_is_not_reported_as_model_argument_error():
    def fail(*_):
        raise ValueError("向量维度错误")

    api = ScriptedAPI({"tool_calls": [tool("search_runbook", {"query": "连接池"})]}, {"content": "工具错误，证据不足。"})
    result = run_tool_loop("查询", api, fail)
    assert result["tool_events"][0]["result"]["status"] == "error"


def test_missing_data_remains_explicit_in_model_input():
    api = ScriptedAPI({"tool_calls": [tool("query_fault_history", {"error_code": "ERR_MISSING"})]}, {"content": "无匹配证据，无法确认根因。"})
    run_tool_loop("查询历史", api, lambda *_: {"status": "no_data", "records": []})
    assert json.loads(api.requests[-1]["messages"][-1]["content"]) == {"status": "no_data", "records": []}


def test_run_id_and_tenant_isolation_for_non_streaming(client, admin_headers):
    sid = new_session(client, admin_headers)
    response = client.post("/api/agent/chat", json={"session_id": sid, "message": "ERR_5003 连接池怎么排查"}, headers=admin_headers)
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    trace = client.get(f"/api/agent/runs/{run_id}", headers=admin_headers)
    assert trace.status_code == 200 and trace.json()["data"]["backends"]["llm"] == "MockLLM"
    assert "openai_api_key" not in json.dumps(trace.json()).lower()
    other = register(client, "t10_other_tenant", "test123456")
    assert client.get(f"/api/agent/runs/{run_id}", headers=other).status_code == 404


def test_every_sse_event_has_one_server_run_id(client, admin_headers):
    sid = new_session(client, admin_headers)
    response = client.post("/api/agent/chat/stream", json={"session_id": sid, "message": "连接池耗尽怎么排查"}, headers=admin_headers)
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events and all(e.get("run_id") for e in events)
    assert len({e["run_id"] for e in events}) == 1
    run = client.get(f"/api/agent/runs/{events[0]['run_id']}", headers=admin_headers)
    assert run.status_code == 200 and run.json()["status"] == "answered"


def test_explicit_read_only_request_does_not_request_ticket_approval(client, admin_headers):
    sid = new_session(client, admin_headers)
    response = client.post("/api/agent/chat", json={"session_id": sid, "message": "查询order-service指标，不要创建工单"}, headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["intent"] == "diagnose" and "approval" not in response.json()

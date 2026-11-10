import asyncio
import json
from types import SimpleNamespace

from mcp import Client

from app.agent.retrieval_agent import run_retrieval_agent
from tests.test_t10_tool_loop import ScriptedAPI, tool


def test_mcp_rejects_identity_override_writes_and_invalid_credentials(monkeypatch, admin_headers):
    from app import mcp_server

    observed = []
    monkeypatch.setenv("OPSPILOT_MCP_TOKEN", admin_headers["Authorization"].removeprefix("Bearer "))
    monkeypatch.setenv("OPSPILOT_MCP_EVALUATION_TIME", "1001")
    monkeypatch.setattr(mcp_server, "read_logs", lambda tenant_id, **args: observed.append((tenant_id, args)) or {"status": "ok", "records": []})

    async def exercise():
        async with Client(mcp_server.server) as client:
            tools = (await client.list_tools()).tools
            assert {t.name for t in tools} == {"query_logs", "query_service_metrics"}
            assert all(t.annotations.read_only_hint for t in tools)
            result = await client.call_tool("query_logs", {"args": {"service": "order-service", "window_minutes": 5}})
            assert not result.is_error and result.structured_content["status"] == "ok"
            assert (await client.call_tool("query_logs", {"args": {"service": "order-service", "tenant_id": "other"}})).is_error
            assert (await client.call_tool("create_ticket", {})).is_error
            monkeypatch.setenv("OPSPILOT_MCP_TOKEN", "invalid-token")
            assert (await client.call_tool("query_logs", {"args": {"service": "order-service"}})).is_error

    asyncio.run(exercise())
    assert len(observed) == 1 and observed[0][0] == "default" and observed[0][1]["evaluation_time"] == 1001


def test_retrieval_child_cannot_query_metrics_and_returns_original_selected_evidence():
    seen = []
    api = ScriptedAPI(
        {"tool_calls": [tool("query_service_metrics", {"service": "order-service"}, "forbidden")]},
        {"tool_calls": [tool("search_runbook", {"query": "连接获取超时"}, "search")]},
        {"content": json.dumps({"summary": "应检查连接池等待队列", "citations": [1], "gaps": ["缺少手册知识"] * 8}, ensure_ascii=False)},
    )
    hit = {"chunk_id": "actual-chunk", "doc_title": "连接池手册", "text": "检查连接池等待队列"}
    result = run_retrieval_agent("查连接池手册", api, lambda args: seen.append(args) or {"hits": [hit], "status": "ok"})
    assert result["status"] == "ok" and result["hits"] == [hit] and len(seen) == 1
    assert result["child_tool_calls"] == 1
    assert result["gaps_truncated"] is True and len(result["gaps"]) == 5
    first_reply = next(m for m in api.requests[1]["messages"] if m["role"] == "tool")
    assert json.loads(first_reply["content"])["status"] == "invalid_arguments"


def test_retrieval_child_cannot_fabricate_citation():
    api = ScriptedAPI({"content": '{"summary":"编造手册","citations":[99],"gaps":[]}'})
    result = run_retrieval_agent("检索", api, lambda _: {"hits": []})
    assert result["status"] == "invalid_report" and result["hits"] == []


def test_retrieval_child_preserves_all_selected_original_citations():
    api = ScriptedAPI(
        {"tool_calls": [tool("search_runbook", {"query": "第一条线索"}, "first")]},
        {"tool_calls": [tool("search_runbook", {"query": "补查另一手册"}, "second")]},
        {"content": '{"summary":"五条依据","citations":[1,2,3,4,5],"gaps":[]}'},
    )
    batches = iter([[{"chunk_id": str(i), "text": str(i)} for i in range(3)],
                    [{"chunk_id": str(i), "text": str(i)} for i in range(3, 5)]])
    result = run_retrieval_agent("整理依据", api, lambda _: {"hits": next(batches)}, top_k=3)
    assert result["status"] == "ok" and {h["chunk_id"] for h in result["hits"]} == {"0", "1", "2", "3", "4"}


def test_each_investigation_can_delegate_only_once(monkeypatch):
    from app.agent import retrieval_agent, tools

    calls = []
    monkeypatch.setattr("app.services.model_api.ChatAPI", lambda *_: object())
    monkeypatch.setattr(retrieval_agent, "run_retrieval_agent", lambda *args, **kwargs: calls.append(args[0]) or {
        "status": "ok", "summary": "手册依据", "hits": [{"chunk_id": "actual", "doc_title": "手册", "text": "依据"}]})
    settings = SimpleNamespace(retrieval_subagent_enabled=True, openai_api_key="offline", openai_base_url="offline",
                               chat_model="offline", tool_timeout_seconds=1)
    execute = tools.make_tool_executor(SimpleNamespace(settings=settings), "default")
    first = execute("delegate_retrieval", {"query": "连接池", "top_k": 3})
    second = execute("delegate_retrieval", {"query": "换一个问题", "top_k": 3})
    assert first["hits"][0]["citation_id"] == 1 and second["status"] == "delegation_limit"
    assert calls == ["连接池"]


def test_mcp_metrics_keep_the_existing_tool_summary_contract(monkeypatch):
    from app.agent.tools import make_tool_executor

    monkeypatch.setattr("app.services.mcp_client.call_observability_tool", lambda *args: {
        "source": {"kind": "prometheus"}, "window": {"start": "a", "end": "b"}, "service": "order-service",
        "status": "no_data", "anomalies": [], "mcp": {"transport": "stdio"},
    })
    result = make_tool_executor(SimpleNamespace(settings=SimpleNamespace(mcp_enabled=True)), "default", "user")(
        "query_service_metrics", {"service": "order-service", "window_minutes": 5})
    assert result["summary"] and "证据不足" in result["summary"] and result["mcp"]["transport"] == "stdio"

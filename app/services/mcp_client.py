"""通过官方MCP客户端发现和调用受限stdio服务，不把工具失败回退成本地成功。"""
import asyncio
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

from app.auth.security import create_access_token
from app.services.run_service import emit, tool_time_left

OBSERVABILITY_TOOLS = {"query_logs", "query_service_metrics"}


def observability_server_params(settings, user_id, tenant_id, evaluation_time):
    if not user_id:
        raise ValueError("MCP调用必须来自认证用户")
    env = {name.upper(): str(getattr(settings, name)) for name in (
        "database_url", "jwt_secret", "jwt_algorithm", "metrics_provider", "logs_url",
        "prometheus_url", "prometheus_timeout_seconds",
    )}
    env.update(OPSPILOT_MCP_TOKEN=create_access_token(user_id, "", tenant_id, "viewer", purpose="mcp"),
               OPSPILOT_MCP_EVALUATION_TIME=str(evaluation_time))
    return StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"],
                                 cwd=Path(__file__).resolve().parents[2], env=env)


def call_observability_tool(settings, user_id, tenant_id, name, args, evaluation_time):
    if name not in OBSERVABILITY_TOOLS:
        raise ValueError("MCP仅允许日志和指标查询")
    params = observability_server_params(settings, user_id, tenant_id, evaluation_time)
    timeout = tool_time_left() or settings.tool_timeout_seconds

    async def call():
        async with asyncio.timeout(timeout):
            async with Client(params, read_timeout_seconds=timeout) as client:
                discovered = {tool.name for tool in (await client.list_tools()).tools}
                if discovered != OBSERVABILITY_TOOLS:
                    raise ValueError("MCP工具清单超出只读约定")
                response = await client.call_tool(name, {"args": args})
                if response.is_error or not isinstance(response.structured_content, dict):
                    raise ValueError("MCP返回工具错误或无结构化证据")
                return {**response.structured_content, "mcp": {
                    "transport": "stdio", "protocol_version": client.protocol_version,
                    "discovered_tools": sorted(discovered),
                }}

    try:
        result = asyncio.run(call())
        emit("mcp_call", {"name": name, "status": result.get("status"), **result["mcp"]})
        return result
    except TimeoutError:
        raise
    except Exception as exc:
        emit("mcp_call", {"name": name, "status": "unavailable", "error_type": type(exc).__name__})
        return {"status": "unavailable", "summary": "MCP只读工具不可用，当前证据不足。", "error_type": type(exc).__name__}

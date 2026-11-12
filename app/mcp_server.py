"""python -m app.mcp_server：仅暴露指标和日志，身份由宿主进程通过私有环境传入。"""
import os
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from app.agent.tool_loop import LogArgs, MetricsArgs
from app.auth.security import decode_token
from app.db.base import SessionLocal
from app.db.models import LoginSession, User, _now
from app.services.log_service import query_logs as read_logs
from app.services.metrics_service import query_service_metrics as read_metrics

server = MCPServer("OpsPilot只读观测工具", log_level="WARNING")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


def authenticated_tenant():
    token = os.environ.get("OPSPILOT_MCP_TOKEN", "")
    claims = decode_token(token)
    with SessionLocal() as db:
        user = db.get(User, claims.get("sub", ""))
        if user is None or user.tenant_id != claims.get("tenant_id"):
            raise ValueError("MCP身份已失效或租户不匹配")
        if claims.get("token_use") == "access":
            session = db.get(LoginSession, claims.get("jti", ""))
            if not session or session.user_id != user.id or session.revoked_at or session.expires_at <= _now():
                raise ValueError("MCP登录已失效")
        elif claims.get("token_use") != "mcp":
            raise ValueError("MCP令牌用途不匹配")
        return user.tenant_id


@server.tool(annotations=READ_ONLY, structured_output=True)
def query_service_metrics(args: MetricsArgs) -> dict[str, Any]:
    """查询认证租户的测试服务指标；不接受模型指定身份。"""
    tenant_id = authenticated_tenant()
    return read_metrics(**args.model_dump(), tenant_id=tenant_id,
                        evaluation_time=float(os.environ["OPSPILOT_MCP_EVALUATION_TIME"]))


@server.tool(annotations=READ_ONLY, structured_output=True)
def query_logs(args: LogArgs) -> dict[str, Any]:
    """查询认证租户的实际测试请求日志，与指标共享调查时间终点。"""
    tenant_id = authenticated_tenant()
    return read_logs(tenant_id, **args.model_dump(exclude_none=True),
                     evaluation_time=float(os.environ["OPSPILOT_MCP_EVALUATION_TIME"]))


if __name__ == "__main__":
    server.run(transport="stdio")

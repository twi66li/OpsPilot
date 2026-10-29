# API 手册

Base URL：`http://localhost:8000`。除 `/health`、`/auth/*` 外均需 `Authorization: Bearer <token>`。

交互式文档：启动后访问 `/docs`（Swagger UI）。

## 认证

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | /auth/register | 公开 | 注册（默认 viewer），`{"username","password","tenant_id"}` |
| POST | /auth/login | 公开 | 登录，返回 `access_token` |
| GET | /auth/me | 任意 | 当前用户信息 |

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
```

## 知识库 /api/kb

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | /api/kb/documents | admin/engineer | 入库 `{"title","content","source"}`，返回分块数 |
| GET | /api/kb/documents | 任意 | 文档列表（租户内） |
| GET | /api/kb/documents/{id} | 任意 | 文档详情 |
| DELETE | /api/kb/documents/{id} | admin/engineer | 删除（同步清理向量与 BM25） |
| POST | /api/kb/search | 任意 | 检索 `{"query","mode":"dense|bm25|hybrid","top_k"}` |

`/api/kb/search` 返回 `hits`（chunk_id/doc_title/text/score）与 `debug`（两路原始命中），可直接对比检索方案。

## 会话 /api/sessions

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/sessions | 创建会话 `{"title"}` |
| GET | /api/sessions | 会话列表 |
| GET | /api/sessions/{id} | 会话详情 + 全部消息 |

## Agent /api/agent

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | /api/agent/chat | 任意 | 非流式诊断，返回最终结果 + 全部过程事件 |
| POST | /api/agent/chat/stream | 任意 | **SSE 流式诊断**（节点状态/检索/工具/回答/审批） |
| GET | /api/agent/sessions/{id}/pending-approval | 任意 | 查询会话待审批单 |
| POST | /api/agent/approvals/{id}/decide | admin/engineer | 审批 `{"approved":bool,"comment"}`，恢复 LangGraph 中断流程 |
| GET | /api/agent/tools | 任意 | 工具清单 |

请求体：`{"session_id":"...","message":"..."}`。有频率限制（默认 60 次/分钟/用户，Redis 计数，超限 429）。

### SSE 事件类型

`start → node_status*(route_intent/retrieve/grade/rewrite/call_tools/generate/approval_gate/create_ticket) → retrieval → grade → rewrite? → tool* → answer → (approval_required)? → (ticket_created|ticket_rejected)? → final → done`

字段详见 [ARCHITECTURE.md](ARCHITECTURE.md) 第 5 节。

### 端到端示例（Python）

```python
import json, urllib.request

def call(method, path, token=None, body=None):
    r = urllib.request.Request("http://localhost:8000" + path, method=method)
    if token: r.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    if data: r.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(r, data).read().decode()

tok = json.loads(call("POST", "/auth/login", body={"username":"admin","password":"admin123"}))["access_token"]
sid = json.loads(call("POST", "/api/sessions", tok, {"title":"demo"}))["id"]
stream = call("POST", "/api/agent/chat/stream", tok, {"session_id": sid, "message": "帮我创建工单处理网关 502"})
events = [json.loads(l[6:]) for l in stream.splitlines() if l.startswith("data: ")]
approval = next(e for e in events if e["type"] == "approval_required")
result = json.loads(call("POST", f"/api/agent/approvals/{approval['approval_id']}/decide", tok, {"approved": True, "comment": "ok"}))
print(result["ticket_id"])
```

（或直接运行 `python scripts/smoke_test.py`。）

## 工单 / 运维数据

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| GET | /api/tickets | 任意 | 工单列表（租户内） |
| GET | /api/tickets/{id} | 任意 | 工单详情 |
| POST | /api/admin/faults | admin/engineer | 录入故障记录 |
| GET | /api/admin/faults?service=&error_code= | 任意 | 查询故障记录 |
| GET | /api/metrics/{service}?window=60 | 任意 | 服务实时指标（模拟 Prometheus，含异常项） |

## 管理 /api/admin

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | /api/admin/users | admin | 创建用户 `{"username","password","role","tenant_id"}` |

## 错误码约定

- 401 未认证 / Token 无效；403 角色不足；404 资源不存在（含跨租户访问）；400 参数错误 / 审批单已处理；409 审批流程态丢失（服务重启后对旧中断恢复）。

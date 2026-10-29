"""端到端冒烟测试：对运行中的服务验证 登录 → SSE 诊断流 → 工单审批。

用法：
  python scripts/smoke_test.py                # 默认 http://127.0.0.1:8000
  BASE_URL=http://127.0.0.1:8010 python scripts/smoke_test.py

前置：已执行 python -m scripts.seed。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")


def req(method, path, token=None, body=None):
    r = urllib.request.Request(BASE + path, method=method)
    if token:
        r.add_header("Authorization", "Bearer " + token)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, data, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def parse_sse(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def main() -> int:
    status, body = req("GET", "/health")
    print("health:", status, body)
    if status != 200:
        return 1

    status, body = req("POST", "/auth/login", body={"username": "admin", "password": "admin123"})
    print("login:", status)
    if status != 200:
        return 1
    tok = json.loads(body)["access_token"]

    status, body = req("POST", "/api/sessions", tok, {"title": "smoke"})
    sid = json.loads(body)["id"]
    print("session:", status, sid[:8])

    status, body = req("POST", "/api/agent/chat/stream", tok, {"session_id": sid, "message": "ERR_5003 连接池耗尽怎么处理？"})
    events = parse_sse(body)
    types = [e["type"] for e in events]
    final = next(e for e in events if e["type"] == "final")
    print("诊断 SSE:", status, "| 事件:", types)
    print("  状态:", final["status"], "| 引用:", final["citations"])
    ok_diag = final["status"] == "answered" and {"start", "retrieval", "tool", "answer", "final", "done"} <= set(types)

    status, body = req("POST", "/api/agent/chat/stream", tok, {"session_id": sid, "message": "帮我创建工单处理网关 502 问题"})
    events = parse_sse(body)
    final = next(e for e in events if e["type"] == "final")
    approval = final.get("approval") or {}
    print("工单审批:", final["status"], "| approval_id:", approval.get("approval_id", "")[:8])
    ok_approval = final["status"] == "approval_required" and bool(approval.get("approval_id"))

    status, body = req(
        "POST", f"/api/agent/approvals/{approval['approval_id']}/decide", tok, {"approved": True, "comment": "冒烟测试审批"}
    )
    print("审批决定:", status, "| 工单:", json.loads(body).get("ticket_id", "")[:8])
    ok_decide = status == 200

    print("\n结果：", "✅ 全部通过" if (ok_diag and ok_approval and ok_decide) else "❌ 存在失败项")
    return 0 if (ok_diag and ok_approval and ok_decide) else 1


if __name__ == "__main__":
    sys.exit(main())

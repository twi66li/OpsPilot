"""SSE 流式接口测试。"""
import json

from tests.conftest import new_session


def _parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


def test_stream_emits_node_and_final_events(client, admin_headers):
    session_id = new_session(client, admin_headers)
    with client.stream(
        "POST",
        "/api/agent/chat/stream",
        json={"session_id": session_id, "message": "ERR_5003 连接池耗尽如何处理？"},
        headers=admin_headers,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        text = "".join(chunk for chunk in resp.iter_text())

    events = _parse_sse(text)
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert types[-1] == "done"
    assert "node_status" in types
    assert "retrieval" in types
    assert "tool" in types
    assert "answer" in types

    final = next(e for e in events if e["type"] == "final")
    assert final["status"] == "answered"
    assert final["answer"]


def test_stream_approval_flow(client, admin_headers):
    session_id = new_session(client, admin_headers)
    with client.stream(
        "POST",
        "/api/agent/chat/stream",
        json={"session_id": session_id, "message": "帮我创建工单处理网关 502 问题"},
        headers=admin_headers,
    ) as resp:
        text = "".join(chunk for chunk in resp.iter_text())
    events = _parse_sse(text)
    final = next(e for e in events if e["type"] == "final")
    assert final["status"] == "approval_required"
    assert any(e["type"] == "approval_required" for e in events)

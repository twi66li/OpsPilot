import uuid

import httpx
from openai import APIConnectionError

from app.config import get_settings
from app.container import get_container
from app.services.budget import BudgetExceededError
from app.services.budget_pg import DatabaseBudgetLedger
from app.services.model_api import reserve_for_channel, settle_for_channel
from app.services.visitor_service import VISITOR_COOKIE, VISITOR_PASSWORD, VISITOR_USERNAME
from tests.conftest import login, register

ROOT = "/api/assistant/conversations"


def new_chat(client, headers):
    response = client.post(ROOT, headers=headers, json={"title": "首次提问"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_plain_chat_connection_failure_reports_model_error(client, monkeypatch):
    user = register(client, "assistant_connection_error", "quota-pass-123")
    session = new_chat(client, user)

    class UnreachableModel:
        def tool_turn(self, messages, tools, *, allow_tools):
            raise APIConnectionError(request=httpx.Request("POST", "https://model.example.test/v1/chat/completions"))

    monkeypatch.setattr(get_container().llm, "_api", UnreachableModel(), raising=False)
    response = client.post("/api/agent/chat/stream", headers=user, json={
        "session_id": session, "message": "hi", "workflow": "chat",
        "request_id": str(uuid.uuid4()), "background": True,
    })
    assert response.status_code == 200
    assert "连接平台模型失败，请稍后重试" in response.text
    history = client.get(f"{ROOT}/{session}", headers=user).json()["messages"]
    assert [(message["role"], message["content"]) for message in history] == [("user", "hi")]


def test_guest_chat_is_private_and_counts_each_thread_once(client, monkeypatch):
    client.cookies.delete(VISITOR_COOKIE, path="/auth/login")
    guest = login(client, VISITOR_USERNAME, VISITOR_PASSWORD)
    session = new_chat(client, guest)
    assert client.get("/api/assistant/usage", headers=guest).json()["chats"] == {"limit": 3, "used": 0}

    ledger = DatabaseBudgetLedger(scope="assistant-test-model", limit=100_000, model_limits={"test-chat": 100_000})

    class FakeModel:
        def tool_turn(self, messages, tools, *, allow_tools):
            assert not tools and not allow_tools
            assert messages[-1]["role"] == "user"
            guard, hold = reserve_for_channel([], ledger, "tool_chat", "test-chat", 38)
            settle_for_channel({"usage": {"total_tokens": 17}}, guard, hold)
            return {"content": "直接回答普通聊天"}

    monkeypatch.setattr(get_container().llm, "_api", FakeModel(), raising=False)
    payload = {"session_id": session, "message": "网站返回 502 是什么意思？", "workflow": "chat",
               "request_id": str(uuid.uuid4()), "background": True}
    for _ in range(2):
        response = client.post("/api/agent/chat/stream", headers=guest, json=payload)
        assert response.status_code == 200 and "直接回答普通聊天" in response.text
    usage = client.get("/api/assistant/usage", headers=guest).json()
    assert usage["chats"] == {"limit": 3, "used": 1}
    assert usage["tokens"] == {"limit": 50_000, "used": 17}
    assert usage["uploads"] is None

    url = ROOT + "/" + session
    assert client.patch(url, headers=guest, json={"title": "已改名"}).status_code == 200
    rejoined = login(client, VISITOR_USERNAME, VISITOR_PASSWORD)
    assert client.get(url, headers=rejoined).json()["title"] == "已改名"
    assert len(client.get(url, headers=rejoined).json()["messages"]) == 2

    client.cookies.delete(VISITOR_COOKIE, path="/auth/login")
    other = login(client, VISITOR_USERNAME, VISITOR_PASSWORD)
    assert client.get(ROOT, headers=other).json() == []
    assert client.get(url, headers=other).status_code == 404
    assert client.patch(url, headers=other, json={"title": "不能跨访客改名"}).status_code == 404
    assert client.get("/api/support/sites/visitor-example-site", headers=other).status_code == 200

    body = {"request_id": str(uuid.uuid4()), "message": "我用自己的模型"}
    for _ in range(2):
        direct = ROOT + "/" + new_chat(client, guest) + "/personal-turn"
        assert client.post(direct, headers=guest, json=body).status_code == 200
        for _ in range(2):
            assert client.post(direct, headers=guest, json={**body, "answer": "自带模型回复"}).status_code == 200
    assert client.delete(url, headers=guest).status_code == 200
    extra = ROOT + "/" + new_chat(client, guest) + "/personal-turn"
    assert client.post(extra, headers=guest, json=body).status_code == 429
    assert client.get("/api/assistant/usage", headers=guest).json()["tokens"]["used"] == 17


def test_ordinary_user_uploads_ten_distinct_files_and_admin_has_no_personal_cap(client, admin_headers, tmp_path, monkeypatch):
    user = register(client, "assistant_upload_quota", "quota-pass-123")
    monkeypatch.setattr(get_settings(), "kb_storage_path", str(tmp_path))
    monkeypatch.setattr("app.api.router_kb.UPLOADS_PER_MINUTE", 100)

    def upload(index):
        return client.post("/api/support/documents/upload", headers=user,
                           files={"file": (f"manual-{index}.txt", f"instructions {index}".encode(), "text/plain")})

    for index in range(10):
        response = upload(index)
        assert response.status_code == 202, response.text
    assert upload(0).json()["duplicate"] is True
    over = upload(10)
    assert over.status_code == 429 and "零点" in over.json()["detail"]
    usage = client.get("/api/assistant/usage", headers=user).json()
    assert usage["uploads"] == {"used": 10, "limit": 10}
    assert usage["chats"] == {"used": 0, "limit": 20}
    assert usage["tokens"] == {"used": 0, "limit": 10_000_000}
    assert client.get("/api/assistant/usage", headers=admin_headers).json()["tokens"] is None


def test_case_draft_tracks_explicitly_and_retrieval_budget_returns_429(client, monkeypatch):
    user = register(client, "assistant_draft_case", "quota-pass-123")
    record = client.post("/api/support/cases", headers=user, json={"title": "排查草稿", "draft": True}).json()
    query = "/api/workspace/cases?case_kind=problem"
    assert client.get(query, headers=user).json()["total"] == 0
    tracked = client.post(f"/api/support/cases/{record['id']}/track", headers=user)
    assert tracked.status_code == 200 and tracked.json()["draft"] is False
    assert client.get(query, headers=user).json()["total"] == 1

    def quota_exhausted(*args):
        raise BudgetExceededError("今天的模型额度不足")

    monkeypatch.setattr("app.api.maintenance_workspace.retrieve_for_case", quota_exhausted)
    response = client.post("/api/maintenance/retrieval-test", headers=user, json={"query": "超时"})
    assert response.status_code == 429 and "额度不足" in response.json()["detail"]

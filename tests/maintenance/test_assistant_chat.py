import json
import uuid

import httpx
from openai import APIConnectionError

from app.config import get_settings
from app.container import get_container
from app.services.budget import BudgetExceededError
from app.services.budget_pg import DatabaseBudgetLedger
from app.services.ingestion_service import process_version
from app.services.model_api import reserve_for_channel, settle_for_channel
from app.services.visitor_service import VISITOR_COOKIE, VISITOR_PASSWORD, VISITOR_USERNAME
from tests.conftest import admin_create_user, login, register

ROOT = "/api/assistant/conversations"


def new_chat(client, headers):
    response = client.post(ROOT, headers=headers, json={"title": "首次提问"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_plain_chat_retrieves_and_preserves_owned_manual_excerpts(client, admin_headers, monkeypatch, tmp_path):
    owner = admin_create_user(client, admin_headers, "chat-manual-owner", "quota-pass-123", "viewer")
    other = admin_create_user(client, admin_headers, "chat-manual-other", "quota-pass-123", "viewer")
    container = get_container()
    monkeypatch.setattr(container.settings, "kb_storage_path", str(tmp_path))
    monkeypatch.setattr("app.api.router_kb.enqueue", process_version)
    manual = "\n\n".join(f"## {part}\n\n浪潮系统的{part}说明。" for part in ("控制节点", "计算节点", "共享存储", "网络组件"))
    upload = client.post("/api/support/documents/upload", headers=owner,
                         files={"file": ("incloud.md", manual.encode(), "text/markdown")})
    assert upload.status_code == 202, upload.text
    doc_id = upload.json()["doc_id"]
    prompts = []

    class FakeModel:
        def tool_turn(self, messages, tools, *, allow_tools):
            assert not tools and not allow_tools
            prompts.append(messages[-1]["content"])
            return {"content": "手册描述了系统组件 [1]。"}

    monkeypatch.setattr(container.llm, "_api", FakeModel(), raising=False)

    def ask(user, session, question):
        response = client.post("/api/agent/chat/stream", headers=user, json={
            "session_id": session, "message": question, "workflow": "chat",
            "request_id": str(uuid.uuid4()), "background": True,
        })
        assert response.status_code == 200, response.text
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        return next(event for event in events if event["type"] == "final")

    session = new_chat(client, owner)
    result = ask(owner, session, "介绍下我的浪潮系统")
    hits = result["hits"]
    assert result["intent"] == "chat_retrieve"
    assert [hit["citation_id"] for hit in hits] == [1, 2, 3]
    assert all(hit["doc_id"] == doc_id for hit in hits)
    excerpts = json.loads(prompts[0].split("\n\n手册参考片段：\n", 1)[1])
    assert [excerpt["片段"] for excerpt in excerpts] == [hit["text"] for hit in hits]
    history = client.get(f"{ROOT}/{session}", headers=owner).json()["messages"]
    assert history[0]["content"] == "介绍下我的浪潮系统"
    assert history[-1]["meta"]["hits"] == hits
    assert client.get(f"/api/kb/chunks/{hits[0]['chunk_id']}", headers=owner).status_code == 200
    assert client.get(f"/api/kb/chunks/{hits[0]['chunk_id']}", headers=other).status_code == 404

    assert ask(owner, session, "你好")["hits"] == []
    assert prompts[-1] == "你好"
    missing = ask(other, new_chat(client, other), "按我上传的手册介绍系统")
    assert missing["hits"] == [] and "没有在你已生效的手册中找到" in missing["answer"]
    assert len(prompts) == 2


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
    assert all(thread.get("read_only") and thread["id"] != session for thread in client.get(ROOT, headers=other).json())
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


def test_in_chat_evidence_diagnosis_and_review_loop(client):
    user = register(client, "assistant_in_chat_diag", "quota-pass-123")
    conv = new_chat(client, user)

    # 1. 先有一条普通聊天消息，后续诊断自动带上上下文
    turn = {"request_id": str(uuid.uuid4()), "message": "下单接口偶发 502"}
    assert client.post(f"{ROOT}/{conv}/personal-turn", headers=user, json=turn).status_code == 200
    assert client.post(f"{ROOT}/{conv}/personal-turn", headers=user, json={**turn, "answer": "请贴一下报错日志"}).status_code == 200

    # 2. 在同一对话内发起证据诊断：自动脱敏、创建排查卡片并返回 workflow=diagnose
    req1 = str(uuid.uuid4())
    diag1 = client.post(
        f"{ROOT}/{conv}/diagnose",
        headers=user,
        json={
            "request_id": req1,
            "text": "2026-11-26T10:00:00+08:00 ERROR QueuePool limit password=my-secret-pw\n10.0.0.8 GET /orders HTTP 502",
            "label": "下单报错日志",
        },
    )
    assert diag1.status_code == 200, diag1.text
    payload1 = diag1.json()
    case_id = payload1["case"]["id"]
    assert payload1["workflow"] == "diagnose"
    assert payload1["redaction"]["replacements"] == 1
    assert "my-secret-pw" not in payload1["case"]["evidence"][0]["text"]
    assert "下单接口偶发 502" in payload1["case"]["problem"]["symptom"]

    # 幂等重试不重复追加证据
    dup = client.post(f"{ROOT}/{conv}/diagnose", headers=user, json={"request_id": req1, "text": "ignored"})
    assert dup.status_code == 200 and len(dup.json()["case"]["evidence"]) == 1

    # 3. 模拟已有诊断报告并确认检查方案后，再次提交证据自动进入 review 阶段且复用同一 case_id
    from app.db.base import SessionLocal
    from app.db.models import SupportCase

    with SessionLocal() as db:
        row = db.get(SupportCase, case_id)
        row.data = {
            **row.data,
            "revision": 1,
            "report": {
                "workflow": "diagnose",
                "summary": "连接池耗尽",
                "observations": [],
                "hypotheses": [],
                "missing_info": [],
                "steps": [{"location": "容器", "action": "docker logs app", "normal": "正常", "abnormal": "超时", "risk": "只读", "rollback": ""}],
                "references": [],
                "verification": {"verdict": "inconclusive", "evidence_ids": [], "reason": "待复测"},
            },
        }
        db.commit()
    assert client.post(f"/api/support/cases/{case_id}/confirm-plan", headers=user).status_code == 200

    diag2 = client.post(
        f"{ROOT}/{conv}/diagnose",
        headers=user,
        json={"request_id": str(uuid.uuid4()), "text": "2026-11-26T10:05:00+08:00 GET /orders HTTP 200", "step_index": 0},
    )
    assert diag2.status_code == 200, diag2.text
    payload2 = diag2.json()
    assert payload2["case"]["id"] == case_id
    assert payload2["workflow"] == "review"
    assert payload2["case"]["evidence"][-1]["kind"] == "check_result"

    # 4. 读取聊天历史时一并返回关联 cases 供刷新恢复卡片；访客禁止提交诊断证据
    hist = client.get(f"{ROOT}/{conv}", headers=user).json()
    assert case_id in hist["cases"]
    assert len(hist["cases"][case_id]["evidence"]) == 2

    client.cookies.delete(VISITOR_COOKIE, path="/auth/login")
    guest = login(client, VISITOR_USERNAME, VISITOR_PASSWORD)
    guest_conv = new_chat(client, guest)
    assert client.post(
        f"{ROOT}/{guest_conv}/diagnose", headers=guest, json={"request_id": str(uuid.uuid4()), "text": "ERROR 500"}
    ).status_code == 403


def test_custom_model_shares_agent_workflow_and_blocks_ssrf(client, monkeypatch, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import pytest

    from app.db.base import SessionLocal
    from app.db.models import AgentRun, Message
    from app.services.custom_model import (
        HEADER_ENDPOINT,
        HEADER_KEY,
        HEADER_MODEL,
        _ephemeral_runs,
        validate_custom_model_endpoint,
    )

    # 1. 生产模式下严格拦截明文 HTTP、回环、私网与云元数据地址
    for bad in (
        "http://api.deepseek.com/v1",
        "http://127.0.0.1:11434/v1",
        "https://127.0.0.1/v1",
        "https://localhost/v1",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.5/v1",
        "https://user:pass@api.deepseek.com/v1",
    ):
        with pytest.raises(ValueError):
            validate_custom_model_endpoint(bad, allow_loopback=False)

    # 2. 启动本地临时 OpenAI 兼容网关，验证自带模型走同一套后端 Agent（手册检索 + 证据诊断）
    credential = "sk-byok-ephemeral-secret-999"
    seen_calls: list[dict] = []
    diag_evidence_id = [""]

    class FakeOpenAIHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            seen_calls.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            assert self.headers.get("Authorization") == f"Bearer {credential}"
            assert body["model"] == "deepseek-flash"
            last_text = body["messages"][-1]["content"]
            if "手册参考片段" in last_text:
                answer = "依据手册记录，平台监控指标由 ICS Monitor 定时采集 [1]。"
            else:
                # 证据化诊断返回符合 SupportReport 结构的 JSON，验证自带模型同样走确定性脱敏与核对
                ev_id = diag_evidence_id[0]
                answer = json.dumps(
                    {
                        "summary": "应用连接池等待超时导致接口 502。",
                        "observations": [{"evidence_id": ev_id, "line_start": 1, "line_end": 1, "meaning": "连接池耗尽"}],
                        "hypotheses": ["连接池耗尽，尚未证实"],
                        "missing_info": ["连接池上限配置"],
                        "steps": [{
                            "location": "应用容器", "action": "docker logs app --tail 50",
                            "normal": "无超时", "abnormal": "持续超时", "risk": "只读", "rollback": "",
                        }],
                        "references": [],
                        "verification": {"verdict": "sample_recovered", "evidence_ids": [ev_id], "reason": "模型乐观声称"},
                    },
                    ensure_ascii=False,
                )
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunk = {
                    "id": "chatcmpl-byok",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": answer}, "finish_reason": None}],
                }
                end_chunk = {
                    "id": "chatcmpl-byok",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.write(f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                payload = {
                    "id": "chatcmpl-byok",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
                }
                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAIHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        user = register(client, "byok_workflow_user", "quota-pass-123")
        container = get_container()
        monkeypatch.setattr(container.settings, "kb_storage_path", str(tmp_path))
        monkeypatch.setattr("app.api.router_kb.enqueue", process_version)

        uploaded = client.post(
            "/api/support/documents/upload",
            headers=user,
            files={"file": ("InCloudSphere-Monitor.md", "InCloudSphere 6.5.1 使用 ICS Monitor 采集集群监控指标。".encode(), "text/markdown")},
        )
        assert uploaded.status_code == 202, uploaded.text

        byok_headers = {
            **user,
            HEADER_ENDPOINT: f"http://127.0.0.1:{port}/v1",
            HEADER_MODEL: "deepseek-flash",
            HEADER_KEY: credential,
        }

        # 2a. 普通聊天自动触发个人手册检索并流式返回自带模型回复
        conv_id = new_chat(client, user)
        with client.stream(
            "POST",
            "/api/agent/chat/stream",
            headers=byok_headers,
            json={"session_id": conv_id, "message": "我的 InCloudSphere 6.5.1 系统是怎么监控的", "workflow": "chat", "request_id": str(uuid.uuid4())},
        ) as resp:
            assert resp.status_code == 200
            stream_text = "".join(resp.iter_text())
        assert "ICS Monitor" in stream_text
        hist = client.get(f"{ROOT}/{conv_id}", headers=user).json()
        assert hist["messages"][-1]["role"] == "assistant"
        assert hist["messages"][-1]["meta"]["hits"][0]["doc_title"] == "InCloudSphere-Monitor"

        # 2b. 同一套自带模型直接用于证据化诊断（包含服务端脱敏 + 结构化报告核对）
        diag = client.post(
            f"{ROOT}/{conv_id}/diagnose",
            headers=user,
            json={
                "request_id": str(uuid.uuid4()),
                "text": "2026-11-27T10:00:00+08:00 ERROR QueuePool timeout password=db-secret\nGET /orders HTTP 502",
                "label": "接口报错日志",
            },
        ).json()
        case_id = diag["case"]["id"]
        diag_evidence_id[0] = diag["case"]["evidence"][0]["id"]
        diag_resp = client.post(
            "/api/agent/chat",
            headers=byok_headers,
            json={"session_id": case_id, "message": diag["prompt"], "workflow": "diagnose", "request_id": str(uuid.uuid4())},
        )
        assert diag_resp.status_code == 200, diag_resp.text
        report = diag_resp.json()["support_report"]
        assert report["observations"][0]["meaning"] == "连接池耗尽"
        # 自带模型盲目声称已恢复时，仍被 OpsPilot 确定性核对强制收口为 inconclusive
        assert report["verification"]["verdict"] == "inconclusive"
        assert "请先确认检查方案" in report["summary"]

        # 3. 凭据绝不落库，不进内存残留，也不扣平台 Token 额度
        assert len(seen_calls) >= 2
        assert client.get("/api/assistant/usage", headers=user).json()["tokens"]["used"] == 0
        assert not _ephemeral_runs
        with SessionLocal() as db:
            runs_dump = json.dumps([r.data for r in db.query(AgentRun).all()], ensure_ascii=False)
            msgs_dump = json.dumps([(m.content, m.meta) for m in db.query(Message).all()], ensure_ascii=False)
            assert credential not in runs_dump
            assert credential not in msgs_dump
    finally:
        server.shutdown()
        server.server_close()

"""个人排障的使用闭环、资料隔离与恢复证据门槛；不消耗模型额度。"""
import json

from app.container import get_container
from app.services.ingestion_service import process_version
from app.services.support_service import SupportReport, redact, retrieve_for_case, validate_report
from tests.conftest import admin_create_user, register


def report(evidence_id=None):
    return {"summary": "日志提示连接池等待超时，根因仍需检查。",
            "observations": ([{"evidence_id": evidence_id, "line_start": 1, "line_end": 1,
                               "meaning": "发生连接等待超时"}] if evidence_id else []),
            "hypotheses": ["慢操作占用连接，尚未证实"], "missing_info": ["连接池配置和同一时段活动连接"],
            "steps": [{"location": "应用主机", "action": "只读核对连接池配置和进程数", "normal": "容量足够则继续检查慢事务",
                       "abnormal": "记录容量限制并核对并发", "risk": "只读", "rollback": ""}],
            "references": [1], "verification": {"verdict": "sample_recovered", "evidence_ids": [evidence_id] if evidence_id else [],
                                                "reason": "提交的结果显示成功"}}


def test_redaction_preserves_error_and_line_numbers():
    text = ('Authorization: Bearer real-token\n{"password": "complex secret"}\n'
            'postgresql://app:realpass@db/site\napi_key=real-key\nuser@mail.com\nQueuePool timeout')
    preview = redact(text)
    assert all(secret not in preview["text"] for secret in ("real-token", "complex secret", "realpass", "real-key", "user@mail.com"))
    assert preview["text"].count("\n") == text.count("\n")
    assert "QueuePool timeout" in preview["text"]
    assert redact(preview["text"])["text"] == preview["text"]


def test_recovery_requires_new_post_plan_evidence():
    data = report("E1")
    data["missing_info"] = ["框架", "部署方式", "版本", "时间范围"]
    parsed = SupportReport.model_validate(data)
    context = {"evidence": [{"id": "E1", "kind": "initial", "text": "HTTP 200"}]}
    assert validate_report(parsed, context, [])["verification"]["verdict"] == "inconclusive"
    context["plan"] = {"evidence_ids": ["E1"]}
    context["evidence"][0]["kind"] = "check_result"
    assert validate_report(parsed, context, [])["verification"]["verdict"] == "inconclusive"
    context["evidence"].append({"id": "E2", "kind": "check_result", "text": "HTTP 200"})
    data["verification"]["evidence_ids"] = ["E1", "E2"]
    data["observations"][0]["line_end"] = 999
    validated = validate_report(SupportReport.model_validate(data), context, [])
    assert validated["verification"]["verdict"] == "sample_recovered"
    assert validated["observations"] == [] and validated["references"] == []


def test_personal_handbook_is_private_even_with_same_tenant(client, admin_headers, monkeypatch, tmp_path):
    viewer = admin_create_user(client, admin_headers, "personal-viewer", "view123456", "viewer")
    container = get_container()
    monkeypatch.setattr(container.settings, "kb_storage_path", str(tmp_path))
    monkeypatch.setattr("app.api.router_kb.enqueue", process_version)
    uploaded = client.post('/api/support/documents/upload', headers=viewer,
                           files={"file": ("site.md", b"# SITE_ONLY_MANUAL\nprivate rollback details")})
    assert uploaded.status_code == 202, uploaded.text
    version = uploaded.json()
    doc_id, version_id = version["doc_id"], version["version_id"]
    own = client.get(f'/api/kb/versions/{version_id}', headers=viewer)
    assert own.status_code == 200 and own.json()["status"] == "ready"
    for path in (f'/api/kb/versions/{version_id}', f'/api/kb/versions/{version_id}/file',
                 f'/api/kb/documents/{doc_id}', f'/api/kb/chunks/{own.json()["chunks"][0]["chunk_id"]}'):
        assert client.get(path, headers=admin_headers).status_code == 404
    other_hits = client.post('/api/kb/search', headers=admin_headers, json={"query": "SITE_ONLY_MANUAL"}).json()["hits"]
    assert all(h["doc_id"] != doc_id for h in other_hits)
    assert client.post(f'/api/kb/versions/{version_id}/activate', headers=viewer).status_code == 200
    from app.auth.security import decode_token

    identity = decode_token(viewer["Authorization"].split(" ", 1)[1])
    selected = retrieve_for_case(container, identity["tenant_id"], identity["sub"],
                                 {"profile": {"doc_ids": [doc_id]}}, "SITE_ONLY_MANUAL rollback", 5)
    assert any(hit["doc_id"] == doc_id and hit["source_kind"] == "private" for hit in selected)
    assert client.post('/api/support/sites', headers=admin_headers, json={"name": "other", "doc_ids": [doc_id]}).status_code == 400


def test_no_manual_personal_flow_and_evidence_feedback(client, admin_headers, monkeypatch):
    user = register(client, "personal-new-user", "user123456")
    library = client.get('/api/support/library', headers=user).json()
    assert len(library) >= 6 and all(a["url"].startswith("https://") for a in library)
    profile = client.post('/api/support/sites', headers=user, json={"name": "个人站点", "framework": "FastAPI", "database": "PostgreSQL 16"}).json()
    created = client.post('/api/support/cases', headers=user, json={"title": "连接池超时", "site_id": profile["id"]}).json()
    session_id = created["id"]
    assert client.get(f'/api/support/cases/{session_id}', headers=admin_headers).status_code == 404
    client.put(f'/api/support/sites/{profile["id"]}', headers=user, json={"name": "新版站点", "framework": "Django"})
    assert client.get(f'/api/support/cases/{session_id}', headers=user).json()["profile"]["framework"] == "FastAPI"
    assert client.post(f'/api/support/cases/{session_id}/confirm-plan', headers=user).status_code == 409
    assert client.post(f'/api/support/cases/{session_id}/evidence', headers=user, json={"text": "done", "kind": "check_result"}).status_code == 409
    initial = client.post(f'/api/support/cases/{session_id}/evidence', headers=user,
                          json={"text": "QueuePool timeout password=secret-value", "label": "错误日志"}).json()
    assert "secret-value" not in initial["text"]

    class PersonalAPI:
        calls = []

        def tool_turn(self, messages, tools, **kwargs):
            self.calls.append(messages)
            assert {t["function"]["name"] for t in tools} == {"search_runbook", "search_saved_cases"}
            if not any(m["role"] == "tool" for m in messages):
                return {"role": "assistant", "content": "", "tool_calls": [{"id": "source-query", "function": {
                    "name": "search_runbook", "arguments": json.dumps({"query": "QueuePool connection timed out", "top_k": 3})}}]}
            return {"role": "assistant", "content": json.dumps(report(initial["id"]), ensure_ascii=False)}

    fake = PersonalAPI()
    monkeypatch.setattr(get_container().llm, "_api", fake, raising=False)
    result = client.post('/api/agent/chat', headers=user, json={"session_id": session_id, "message": "请分析日志 token=chat-secret"})
    assert result.status_code == 200, result.text
    output = result.json()
    assert output["support_report"]["verification"]["verdict"] == "inconclusive"
    assert output["support_report"]["observations"][0]["quote"] == initial["text"]
    assert output["hits"] and all(h["source_kind"] == "official" for h in output["hits"])
    assert "secret-value" not in json.dumps(fake.calls) and "chat-secret" not in json.dumps(fake.calls)
    assert not output.get("approval")
    assert client.post(f'/api/support/cases/{session_id}/confirm-plan', headers=user).status_code == 200
    followup = client.post(f'/api/support/cases/{session_id}/evidence', headers=user, json={"text": "HTTP/1.1 200 OK", "kind": "check_result"})
    assert followup.status_code == 200
    # 已确认方案之后的新结果才允许模型进行样本范围内的恢复核对。
    updated_report = report(initial["id"])
    updated_report["verification"]["evidence_ids"] = [followup.json()["id"]]
    monkeypatch.setattr(fake, "tool_turn", lambda *args, **kwargs: {"role": "assistant", "content": json.dumps(updated_report, ensure_ascii=False)})
    checked = client.post('/api/agent/chat', headers=user, json={"session_id": session_id, "message": "核对新结果"}).json()
    assert checked["support_report"]["verification"]["verdict"] == "sample_recovered"
    saved = client.post(f'/api/support/cases/{session_id}/save', headers=user, json={"solution": "核对并调整了连接生命周期，重试样本通过", "user_confirmed": True})
    assert saved.status_code == 200 and saved.json()["status"] == "saved"
    assert client.post('/api/agent/chat', headers=user, json={"session_id": session_id, "message": "new"}).status_code == 409
    assert session_id not in {s["id"] for s in client.get('/api/sessions', headers=user).json()}

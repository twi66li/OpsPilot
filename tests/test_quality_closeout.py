import json
import runpy
from pathlib import Path

import httpx
import pytest
from openai import APIStatusError

from app.services.model_api import ChatAPI


def test_chat_model_order_skips_expired_models_and_prefers_earlier_expiry(monkeypatch):
    from types import SimpleNamespace

    from app.services.model_api import _available_chat_models

    monkeypatch.setenv("CHAT_MODEL_CANDIDATES", json.dumps(["later", "earlier", "expired"]))
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(
        chat_model_candidates=[], chat_model_expirations={"later": "2099-02-01", "earlier": "2099-01-01", "expired": "1900-01-01"}
    ))
    assert _available_chat_models("expired") == ["earlier", "later"]
    assert _available_chat_models("expired", {"earlier"}) == ["later"]


@pytest.mark.parametrize("code,status,switch", [
    ("AllocationQuota.FreeTierOnly", 403, True),
    ("AccessDenied", 403, False),
    ("Unauthorized", 401, False),
])
@pytest.mark.parametrize("native", [False, True])
def test_provider_quota_switch_keeps_other_errors(monkeypatch, code, status, switch, native):
    monkeypatch.setenv("CHAT_MODEL_CANDIDATES", json.dumps(["backup"]))
    api = ChatAPI.__new__(ChatAPI)
    api.model, api.calls = "primary", []
    attempted = []

    def recorded(model, *args, **kwargs):
        attempted.append(model)
        if model == "primary":
            raise APIStatusError(code, response=httpx.Response(status, request=httpx.Request("POST", "http://recorded")),
                                 body={"code": code})
        return "recorded backup"

    api._chat_once = recorded
    api._tool_turn_once = recorded
    call = (lambda: api.tool_turn([], [])) if native else (lambda: api.chat("system", "question"))
    if switch:
        assert call() == "recorded backup"
        assert attempted == ["primary", "backup"]
        assert api.calls[0]["to"] == "backup"
    else:
        with pytest.raises(APIStatusError):
            call()
        assert attempted == ["primary"]


def test_missing_window_gold_matches_its_own_source():
    run = Path(__file__).resolve().parents[1] / "eval/night20261102q"
    for frozen in (run / "scenarios_frozen.json", run / "quality_run/scenarios_frozen.json"):
        data = json.loads(frozen.read_text(encoding="utf-8"))
        question = next(q for q in data["questions"] if q["id"] == "heldout-q6")
        doc = next(d for d in data["corpus_manifest"] if d["start"] == question["window_start"])
        raw = (run / "corpus" / doc["file"]).read_text(encoding="utf-8").splitlines()
        count = sum("Node card is not fully functional" in line for line in raw)
        assert question["gold_program"]["covered_count"] == count == 11
        assert len(question["gold_program"]["asked_range"]["missing"]) == 15


def test_launcher_total_cap_follows_budget_file(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "eval/night20261102q/quality_budget.py"
    read_text = Path.read_text

    def adjusted_budget(path, *args, **kwargs):
        if path.name == "budget.json":
            return json.dumps({"project_total_limits": {"p1": 900000},
                               "model_limits": {"p1": {"qwen3.8-27b": 300000}}})
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", adjusted_budget)
    budget = runpy.run_path(str(script))
    assert budget["env_for_launcher"]()["BUDGET_TOKEN_LIMIT"] == "900000"
    assert budget["usage_now"]()["project_total_cap"] == 900000


@pytest.mark.parametrize("support,use_template", [("logs", True), ("runbook", False), ("metrics", False)])
def test_log_only_diagnosis_cannot_invent_operational_state(monkeypatch, support, use_template):
    from app.agent.nodes import DiagnosisNodes
    from app.container import get_container

    container = get_container()
    calls = []
    monkeypatch.setattr(container.llm, "generate_answer", lambda *a, **kw: calls.append(kw) or "MODEL_PATH")
    raw = "\n".join(f"L{n}: 1117838677 2005.06.03 N1 2005-06-03-15.44.37.999605 N1 RAS KERNEL INFO instruction cache parity error corrected" for n in (700, 701)) + "\n"
    hits = [{"doc_title": "raw logs", "text": raw, "chunk_id": "logs"}]
    if support == "runbook":
        hits.append({"doc_title": "vendor procedure", "text": "厂商说明与维修判定流程", "chunk_id": "procedure"})
    tools = [{"name": "query_service_metrics", "summary": "实际指标证据"}] if support == "metrics" else []
    result = DiagnosisNodes(container).generate({"question": "L700-L701 内 instruction cache parity 的根因是什么，需要更换硬件吗？",
        "tenant_id": "default", "session_id": "offline-boundary", "hits": hits, "tool_events": tools})
    if use_template:
        assert not calls
        assert result["generation_policy"] == "log_only_evidence_boundary"
        assert result["range_stats"]["total_lines"] == 2
        assert "无法确认根因" in result["answer"] and "corrected" in result["answer"]
        assert "未导致" not in result["answer"] and "并未中断" not in result["answer"]
    else:
        assert calls and result["answer"].startswith("MODEL_PATH")

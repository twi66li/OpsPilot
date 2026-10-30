"""评测自身只测泄漏、预算、评分失败关闭，不镜像模型答案。"""
import json

import pytest

from eval.agent_eval import DATASET, ReplayBackend, hard_score, sha, validate_dataset


def test_dataset_family_leak_is_rejected(tmp_path):
    cases = [{"id": "a", "split": "dev", "family": "same"}, {"id": "b", "split": "test", "family": "same"}]
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({"files": {path.name: sha(path)}}), encoding="utf-8")
    with pytest.raises(ValueError, match="家族泄漏"):
        validate_dataset(tmp_path)
    assert len(validate_dataset(DATASET)) == 32


def test_replay_does_not_substitute_another_service_and_enforces_budget():
    backend = ReplayBackend()
    missing = backend.call("query_service_metrics", {"service": "ghost-service"})
    assert missing["status"] == "no_data" and not missing["avg"]
    wrong_window = backend.call("query_service_metrics", {"service": "order-service", "window_minutes": 30})
    assert wrong_window["status"] == "no_data"
    assert not backend.call("query_fault_history", {"error_code": "ERR_7999"})["records"]
    assert backend.call("search_runbook", {"query": "Redis"})["status"] == "budget_exhausted"
    assert sum(c["executed"] for c in backend.calls) == 3


def test_tool_score_accepts_multiple_orders_but_rejects_wrong_window_and_extra_tools():
    case = {"required_tools": ["query_service_metrics"], "allowed_tools": ["query_service_metrics", "search_runbook"],
            "service": "order-service", "window_minutes": 5, "search_terms": ["order"]}
    calls = [{"name": "search_runbook", "args": {"query": "order"}, "executed": True},
             {"name": "query_service_metrics", "args": {"service": "order-service", "window_minutes": 5}, "executed": True}]
    assert hard_score(case, {"tools": calls})["parameter_correct"]
    assert hard_score(case, {"tools": list(reversed(calls))})["tool_selection"]
    calls[1]["args"]["window_minutes"] = 60
    assert not hard_score(case, {"tools": calls})["parameter_correct"]
    calls.append({"name": "query_fault_history", "args": {}, "executed": True})
    assert not hard_score(case, {"tools": calls})["tool_selection"]

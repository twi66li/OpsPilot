import json

from app.api import router_eval


def test_default_evaluation_skips_latest_acceptance_and_groups_development(client, admin_headers, tmp_path, monkeypatch):
    records = {
        "night20261031/real_run": {"summary":{"completed":1},"questions":{"q1":{"question":"如何查看日志", "answer":"查看容器日志"}}},
        "night20261110/mcp_probe": {"questions":{"q1":{"question":"开发检查"}}},
        "night20261112/personal_support": {"checks":{"passed":True}},
    }
    for run, data in records.items():
        folder = tmp_path / run
        folder.mkdir(parents=True)
        (folder / "results.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(router_eval, "_EVAL_ROOT", tmp_path)
    listed = client.get("/api/eval/runs", headers=admin_headers).json()
    assert listed["default_run"] == "night20261031/real_run"
    assert len(listed["runs"]) == 1 and listed["runs"][0]["question_count"] == 1
    assert listed["development_count"] == 2
    assert len(client.get("/api/eval/runs?include_development=true", headers=admin_headers).json()["runs"]) == 3
    default = client.get("/api/eval/results", headers=admin_headers).json()
    assert default["id"] == listed["default_run"] and len(default["questions"]) == 1
    diagnostic = client.get("/api/eval/results?run=night20261112/personal_support", headers=admin_headers).json()
    assert diagnostic["questions"] == [] and diagnostic["details"]["checks"]["passed"]
    assert client.get("/api/eval/results?run=../../.env", headers=admin_headers).status_code == 404


def test_empty_evaluation_directory_is_explicit(client, admin_headers, tmp_path, monkeypatch):
    monkeypatch.setattr(router_eval, "_EVAL_ROOT", tmp_path)
    assert client.get("/api/eval/runs", headers=admin_headers).json()["default_run"] == ""
    assert client.get("/api/eval/results", headers=admin_headers).status_code == 404

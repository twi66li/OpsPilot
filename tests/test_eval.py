"""评测模块测试：问题集格式与指标聚合。"""
from pathlib import Path

import yaml

from eval.metrics import aggregate, faithfulness_heuristic, recall_at_k


def test_questions_yaml_valid():
    path = Path(__file__).parent.parent / "eval" / "questions.yaml"
    questions = yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]
    assert len(questions) >= 10
    categories = {q["category"] for q in questions}
    assert "错误码" in categories
    assert "服务名" in categories
    assert "自然语言" in categories
    for q in questions:
        assert q["id"] and q["question"]
        assert isinstance(q.get("expected_docs", []), list)
    assert any(q.get("type") == "workflow" for q in questions)


def test_aggregate_math():
    rows = [
        {"recall": 1.0, "mrr": 1.0, "keyword_coverage": 1.0, "faithfulness": 1.0, "latency_ms": 10.0},
        {"recall": 0.0, "mrr": 0.0, "keyword_coverage": 0.5, "faithfulness": 0.5, "latency_ms": 30.0},
    ]
    summary = aggregate(rows, k=5)
    assert summary["recall_at_5"] == 0.5
    assert summary["mrr"] == 0.5
    assert summary["keyword_coverage"] == 0.75
    assert summary["faithfulness"] == 0.75
    assert summary["avg_ms"] == 20.0
    assert summary["p95_ms"] == 30.0


def test_faithfulness_bounds():
    assert faithfulness_heuristic("", ["任意上下文"]) == 0.0
    assert faithfulness_heuristic("完全无关的一句话。", ["连接池耗尽处理"]) <= 1.0
    assert recall_at_k(["A"], ["A"], 1) == 1.0

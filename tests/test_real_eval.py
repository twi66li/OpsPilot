"""真实评测的关键边界，不消耗外部模型额度。"""
from types import SimpleNamespace

import pytest

from app.agent.llm import OpenAIChatLLM
from app.rag.embeddings import OpenAIEmbedding
from eval.metrics import aggregate, mrr_at_k, recall_at_k
from eval.real_eval import document_hits, error_info, select_on_dev


def test_unanswerable_is_excluded_from_retrieval_metrics():
    assert recall_at_k([], ["noise"], 5) is None
    assert mrr_at_k([], ["noise"], 5) is None
    rows = [
        {"recall": None, "mrr": None, "keyword_coverage": None, "faithfulness": 0, "latency_ms": 1},
        {"recall": 0, "mrr": 0, "keyword_coverage": None, "faithfulness": 0, "latency_ms": 1},
    ]
    result = aggregate(rows, 5)
    assert result["n_retrieval_questions"] == 1
    assert result["recall_at_5"] == 0


def test_document_ranking_and_selection_do_not_use_test_scores():
    hits = [SimpleNamespace(doc_id=key) for key in ["a", "a", "b", "c"]]
    assert [h.doc_id for h in document_hits(hits, 2)] == ["a", "b"]
    assert mrr_at_k(["b"], ["a", "a", "b"], 2) == .5
    rows = [
        {"mode": mode, "split": split, "task_type": "retrieval", "recall": score, "mrr": score}
        for mode, split, score in [("bm25", "dev", 1), ("dense", "dev", 0),
                                  ("hybrid", "dev", 0), ("hybrid_rerank", "dev", 0), ("dense", "test", 1)]
    ]
    assert select_on_dev(rows) == "bm25"


def test_strict_generation_does_not_fall_back(monkeypatch):
    llm = OpenAIChatLLM.__new__(OpenAIChatLLM)
    llm.strict = True

    def fail(*args):
        raise RuntimeError("真实服务失败")

    monkeypatch.setattr(llm, "_chat", fail)
    with pytest.raises(RuntimeError, match="真实服务失败"):
        llm.generate_answer("问题", [{"text": "依据"}], [], [], [])


def test_embedding_dimension_is_sent_and_mismatch_rejected(monkeypatch):
    requested = {}

    def create(**kwargs):
        requested.update(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1, 2, 3])])

    provider = OpenAIEmbedding.__new__(OpenAIEmbedding)
    provider.model, provider.dim, provider.calls = "real-embedding", 2, []
    provider._client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    with pytest.raises(ValueError, match="维度"):
        provider.embed(["文档"])
    assert requested["dimensions"] == 2


def test_error_report_redacts_api_credentials(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "private-value")
    result = error_info(RuntimeError("private-value and sk-another-secret"))
    assert "private-value" not in result["message"]
    assert "sk-another-secret" not in result["message"]


def test_chunks_keep_heading_with_body_and_parent_context():
    from app.rag.chunking import split_into_chunks
    chunks = split_into_chunks("# Kafka 排查\n\n## 现象\n消费延迟。\n\n## 原因\n下游变慢。")
    assert all("Kafka 排查" in chunk for chunk in chunks)
    assert all("消费延迟" in chunk or "下游变慢" in chunk for chunk in chunks)
    assert any("下游变慢" in chunk for chunk in chunks)


def test_resume_does_not_reissue_completed_retrieval(tmp_path):
    from eval.real_eval import MODES, retrieval_rows, save
    rows = [{"id": "q1", "mode": mode, "status": "ok"} for mode in MODES]
    save(tmp_path / "retrieval.json", rows)
    # 完成的用例不应再访问模型或检索器。
    assert retrieval_rows(None, [{"id": "q1", "task_type": "retrieval"}], None, tmp_path) == rows


def test_failed_snapshot_replace_preserves_previous_result(tmp_path, monkeypatch):
    from pathlib import Path

    from eval.real_eval import save
    path = tmp_path / "result.json"
    save(path, {"completed": 10})

    def fail(*args):
        raise OSError("模拟替换失败")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        save(path, {"completed": 11})
    assert '"completed": 10' in path.read_text(encoding="utf-8")


def test_snapshot_recovers_from_temporary_windows_file_lock(tmp_path, monkeypatch):
    from pathlib import Path

    from eval import real_eval
    original = Path.replace
    attempts = []

    def locked_twice(source, target):
        attempts.append(1)
        if len(attempts) <= 2:
            raise PermissionError(13, "临时占用")
        return original(source, target)

    monkeypatch.setattr(Path, "replace", locked_twice)
    monkeypatch.setattr(real_eval.time, "sleep", lambda delay: None)
    path = tmp_path / "result.json"
    real_eval.save(path, {"completed": 11})
    assert len(attempts) == 3
    assert '"completed": 11' in path.read_text(encoding="utf-8")


def test_tool_presence_alone_does_not_pass_answer_contract(monkeypatch):
    from app.services import model_api
    from eval.real_eval import score_tool_answers

    class Judge:
        def __init__(self, *args):
            self.calls = []
            self.client = SimpleNamespace(close=lambda: None)

        def chat(self, *args, **kwargs):
            self.calls.append({"status": "ok"})
            return '{"uses_tool_results":false,"values_consistent":true,"simulation_disclosed":false,"reason":"只给查询建议"}'

    monkeypatch.setattr(model_api, "ChatAPI", Judge)
    row = {"id": "q1", "task_type": "tool", "status": "ok", "passed": True,
           "checks": {"required_tools": True}, "events": [{"type": "final", "answer": "建议查询订单"}]}
    score_tool_answers([row], [{"id": "q1", "question": "查询订单"}], "fake", "fake", "judge")
    assert row["passed"] is False
    assert row["checks"]["answer_matches_tool_output"] is False

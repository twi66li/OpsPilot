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

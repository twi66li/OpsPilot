"""评测指标：Recall@K / MRR@K / 关键词覆盖率 / 启发式忠实度 / 延迟统计。"""
import re

from app.rag.embeddings import tokenize


def recall_at_k(expected: list[str], hit_titles: list[str], k: int) -> float:
    """期望文档出现在 Top-K 命中的比例。无期望文档时记 1.0。"""
    if not expected:
        return 1.0
    top = set(hit_titles[:k])
    return sum(1 for doc in expected if doc in top) / len(expected)


def mrr_at_k(expected: list[str], hit_titles: list[str], k: int) -> float:
    """首个期望文档的倒数排名均值。无期望文档时记 1.0。"""
    if not expected:
        return 1.0
    for rank, title in enumerate(hit_titles[:k], start=1):
        if title in expected:
            return 1.0 / rank
    return 0.0


def keyword_coverage(answer: str, keywords: list[str]) -> float | None:
    """回答中命中的期望关键词比例（忠实度代理指标）。无关键词时返回 None。"""
    if not keywords:
        return None
    answer_lower = answer.lower()
    hit = sum(1 for kw in keywords if kw.lower() in answer_lower)
    return hit / len(keywords)


def faithfulness_heuristic(answer: str, contexts: list[str]) -> float:
    """启发式忠实度：答案中能被检索上下文（词面重合）支撑的句子占比。

    生产环境配置真实 LLM 后，可在 run_eval 中切换为 LLM-as-judge。
    """
    if not answer.strip():
        return 0.0
    sentences = [s.strip() for s in re.split(r"[。！？!?\n]", answer) if s.strip()]
    if not sentences:
        return 0.0
    context_tokens = set()
    for ctx in contexts:
        context_tokens.update(tokenize(ctx))
    supported = 0
    for sentence in sentences:
        tokens = [t for t in tokenize(sentence) if len(t) >= 2]
        if not tokens:
            supported += 1  # 纯数字/标点句视为可支撑
            continue
        overlap = sum(1 for t in tokens if t in context_tokens)
        if overlap / len(tokens) >= 0.5:
            supported += 1
    return supported / len(sentences)


def latency_stats(latencies_ms: list[float]) -> dict:
    if not latencies_ms:
        return {"avg_ms": 0.0, "p95_ms": 0.0}
    ordered = sorted(latencies_ms)
    p95_index = min(int(len(ordered) * 0.95), len(ordered) - 1)
    return {"avg_ms": round(sum(ordered) / len(ordered), 1), "p95_ms": round(ordered[p95_index], 1)}


def aggregate(rows: list[dict], k: int) -> dict:
    """把逐条评测结果聚合为方案级指标。"""
    n = len(rows) or 1
    recalls = [r["recall"] for r in rows]
    mrrs = [r["mrr"] for r in rows]
    covs = [r["keyword_coverage"] for r in rows if r["keyword_coverage"] is not None]
    faiths = [r["faithfulness"] for r in rows]
    stats = latency_stats([r["latency_ms"] for r in rows])
    return {
        "n_questions": len(rows),
        f"recall_at_{k}": round(sum(recalls) / n, 4),
        "mrr": round(sum(mrrs) / n, 4),
        "keyword_coverage": round(sum(covs) / len(covs), 4) if covs else None,
        "faithfulness": round(sum(faiths) / len(faiths), 4),
        **stats,
    }

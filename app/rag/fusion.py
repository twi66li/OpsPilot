"""RRF（Reciprocal Rank Fusion）多路检索融合。"""


def rrf_fuse(result_lists: list[list], k: int = 60, top_k: int = 5) -> list:
    """输入多路已排序的检索结果，输出 RRF 融合后的 Top-K。

    score(d) = Σ 1 / (k + rank_i(d))
    """
    fused: dict[str, dict] = {}
    for results in result_lists:
        for rank, chunk in enumerate(results, start=1):
            entry = fused.setdefault(chunk.chunk_id, {"chunk": chunk, "score": 0.0})
            entry["score"] += 1.0 / (k + rank)
    ranked = sorted(fused.values(), key=lambda e: e["score"], reverse=True)[:top_k]
    out = []
    for entry in ranked:
        chunk = entry["chunk"]
        chunk.score = entry["score"]
        out.append(chunk)
    return out

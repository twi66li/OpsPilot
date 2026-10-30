"""重排序：heuristic（离线词面重合，默认）与 Cross-Encoder（可选，需安装 sentence-transformers）。"""
import logging

from app.rag.embeddings import tokenize

logger = logging.getLogger(__name__)


class HeuristicReranker:
    """词面重合度打分：查询 token 在文档中的覆盖率 + 命中数加权。"""

    def rerank(self, query: str, chunks: list, top_k: int = 5) -> list:
        q_tokens = set(tokenize(query))
        scored = []
        for chunk in chunks:
            c_tokens = set(tokenize(chunk.text))
            overlap = len(q_tokens & c_tokens)
            score = overlap / (len(q_tokens) + 1e-6) + 0.1 * overlap
            chunk.score = round(score, 4)
            scored.append(chunk)
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[:top_k]


class CrossEncoderReranker:
    """Cross-Encoder 语义重排序。依赖较重（torch），按需安装 requirements-optional.txt。"""

    def __init__(self, model_name: str):
        from sentence_transformers import CrossEncoder

        self.model = CrossEncoder(model_name)

    def rerank(self, query: str, chunks: list, top_k: int = 5) -> list:
        if not chunks:
            return []
        pairs = [[query, c.text] for c in chunks]
        scores = self.model.predict(pairs)
        for chunk, score in zip(chunks, scores):
            chunk.score = float(score)
        chunks.sort(key=lambda c: c.score, reverse=True)
        return chunks[:top_k]


class DashScopeReranker:
    """DashScope 原生 text-rerank API（T7：正式 API 的真实重排路径，复用评测已验证的 RerankAPI）。

    与 heuristic/cross_encoder 不同：API 调用失败直接抛出、不静默回退词面重排，
    避免真实后端故障被掩盖（strict 语义在 provider 层即成立）。
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        from app.services.model_api import RerankAPI

        self._api = RerankAPI(api_key, base_url, model)
        self.model = model

    @property
    def calls(self) -> list[dict]:
        return self._api.calls

    def rerank(self, query: str, chunks: list, top_k: int = 5) -> list:
        return self._api.rerank(query, chunks, top_k)


def get_reranker(settings):
    if not settings.rerank_enabled:
        return None
    if settings.rerank_provider == "dashscope":
        if not settings.openai_api_key:
            raise ValueError("dashscope 重排需要配置密钥（openai_api_key，即 DashScope API Key）")
        return DashScopeReranker(settings.openai_api_key, settings.openai_base_url, settings.rerank_model)
    if settings.rerank_provider == "cross_encoder":
        try:
            return CrossEncoderReranker(settings.rerank_model)
        except Exception as exc:
            logger.warning("CrossEncoder 加载失败(%s)，退化为 heuristic 重排", exc)
            return HeuristicReranker()
    return HeuristicReranker()

"""Embedding 提供方。

- MockEmbedding：确定性词面哈希向量（离线可用，用于开发/测试/验收，语义≈词面重合）。
- OpenAIEmbedding：任意 OpenAI 兼容 API（生产建议）。
"""
import hashlib
import re

import jieba

jieba.setLogLevel(60)


def tokenize(text: str) -> list[str]:
    """中英混合分词：jieba + ASCII 词元（保证 ERR_5003 / order-service 这类标识可命中）。"""
    text = text.lower()
    tokens = [t for t in jieba.lcut_for_search(text) if t.strip()]
    tokens += re.findall(r"[a-z0-9][a-z0-9_-]*", text)
    return [t for t in tokens if t and t not in ("_", "-")]


class MockEmbedding:
    def __init__(self, dim: int = 256):
        self.dim = dim

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in set(tokenize(text)):
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "little") % self.dim
            vec[idx] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]


class OpenAIEmbedding:
    def __init__(self, api_key: str, base_url: str, model: str, dim: int | None = None):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=60, max_retries=0)
        self.model = model
        self.dim = dim
        self.calls: list[dict] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        import time

        started = time.perf_counter()
        params = {"dimensions": self.dim} if self.dim is not None else {}
        resp = self._client.embeddings.create(model=self.model, input=texts, **params)
        vectors = [item.embedding for item in sorted(resp.data, key=lambda item: item.index)]
        if self.dim is not None and any(len(v) != self.dim for v in vectors):
            raise ValueError("向量模型输出维度与索引配置不一致")
        self.calls.append({"model": self.model, "status": "ok", "elapsed_ms": (time.perf_counter() - started) * 1000,
                           "usage": resp.usage.model_dump()})
        return vectors


def get_embedding_provider(settings):
    if settings.embedding_provider == "openai" and settings.openai_api_key:
        return OpenAIEmbedding(settings.openai_api_key, settings.openai_base_url, settings.embedding_model, settings.embedding_dim)
    if getattr(settings, "model_strict", False):
        raise ValueError("真实评测必须配置 Embedding 服务和密钥")
    return MockEmbedding(settings.embedding_dim)

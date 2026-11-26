"""Embedding 提供方。

- MockEmbedding：确定性词面哈希向量（离线可用，用于开发/测试/验收，语义≈词面重合）。
- OpenAIEmbedding：任意 OpenAI 兼容 API（生产建议）。
"""
import hashlib
import json
import logging
import re

import jieba

from app.services.run_service import check_cancelled, current_run, emit, run_cancelable, tool_time_left

jieba.setLogLevel(60)
logger = logging.getLogger(__name__)


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

        from app.services.budget import BudgetLedgerError, get_budget_guard

        # 真实向量入口：无持久账本配置即 fail closed（MockEmbedding 不经过本类）
        self._budget = get_budget_guard()
        if self._budget is None:
            raise BudgetLedgerError("真实向量入口未配置持久预算账本（BUDGET_LEDGER_PATH/BUDGET_TOKEN_LIMIT），拒绝启动")
        self._client_options = dict(api_key=api_key, base_url=base_url or None, timeout=60, max_retries=0)
        self._client = OpenAI(**self._client_options)
        self.model = model
        self.dim = dim
        self.calls: list[dict] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        import time

        from app.services.budget import estimate_upper_bound
        from app.services.model_api import reserve_for_channel, settle_for_channel

        check_cancelled()
        guard, reservation = reserve_for_channel(
            self.calls, self._budget, "embedding", self.model, estimate_upper_bound(texts, output_cap=0)
        )
        started = time.perf_counter()
        record = {"model": self.model, "status": "error", "usage": {}}
        try:
            params = {"dimensions": self.dim} if self.dim is not None else {}
            remaining = tool_time_left()
            if getattr(current_run(), "cancel_signal", None) is not None:
                from openai import AsyncOpenAI

                async def request():
                    opts = {**self._client_options, "timeout": min(60, remaining) if remaining is not None else 60}
                    async with AsyncOpenAI(**opts) as client:
                        return await client.embeddings.create(model=self.model, input=texts, **params)

                resp = run_cancelable(request)
            else:
                client = self._client.with_options(timeout=remaining) if remaining is not None else self._client
                resp = client.embeddings.create(model=self.model, input=texts, **params)
            usage = getattr(resp, "usage", None)
            record["usage"] = usage.model_dump() if usage else {}
            vectors = [item.embedding for item in sorted(resp.data, key=lambda item: item.index)]
            if self.dim is not None and any(len(v) != self.dim for v in vectors):
                raise ValueError("向量模型输出维度与索引配置不一致")
            record["status"] = "ok"
            return vectors
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            raise
        finally:
            record["elapsed_ms"] = (time.perf_counter() - started) * 1000
            self.calls.append(record)
            logger.info("model_api_usage kind=embedding record=%s", json.dumps(record, ensure_ascii=False))
            emit("model_call", {"kind": "embedding", **record})
            settle_for_channel(record, guard, reservation)


def get_embedding_provider(settings):
    if settings.embedding_provider == "openai" and settings.openai_api_key:
        return OpenAIEmbedding(settings.openai_api_key, settings.openai_base_url, settings.embedding_model, settings.embedding_dim)
    if getattr(settings, "model_strict", False):
        raise ValueError("真实评测必须配置 Embedding 服务和密钥")
    return MockEmbedding(settings.embedding_dim)

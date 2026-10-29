"""Langfuse 可观测：检索 / 重排 / 工具调用 / Token 用量打点。

未配置 Key 或未安装 SDK 时为 no-op，不影响业务链路。
"""
import logging
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文按字、英文按 4 字符 1 token 近似）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + max((len(text) - cjk) // 4, 1)


class _Span:
    def __init__(self, tracer, name: str, metadata: dict):
        self._tracer = tracer
        self.name = name
        self.metadata = metadata
        self._start = time.perf_counter()
        self._lf_span = None
        if tracer._client is not None:
            try:
                self._lf_span = tracer._client.span(name=name, metadata=metadata)
            except Exception:
                self._lf_span = None

    def end(self, output: dict | None = None):
        duration_ms = (time.perf_counter() - self._start) * 1000
        if self._lf_span is not None:
            try:
                self._lf_span.end(output=output or {}, metadata={**self.metadata, "duration_ms": round(duration_ms, 2)})
            except Exception:
                pass
        if self._tracer.verbose:
            logger.info("[trace] span=%s dur=%.1fms meta=%s", self.name, duration_ms, self.metadata)


class Tracer:
    """轻量追踪门面：同时兼容本地日志与 Langfuse 上报。"""

    def __init__(self, settings=None, verbose: bool = True):
        self._client = None
        self.verbose = verbose
        if settings and settings.langfuse_public_key and settings.langfuse_secret_key:
            try:
                from langfuse import Langfuse

                self._client = Langfuse(
                    public_key=settings.langfuse_public_key,
                    secret_key=settings.langfuse_secret_key,
                    host=settings.langfuse_host,
                )
                logger.info("Langfuse 追踪已启用: %s", settings.langfuse_host)
            except Exception as exc:
                logger.warning("Langfuse 初始化失败，追踪退化为本地日志: %s", exc)

    @contextmanager
    def span(self, name: str, **metadata):
        s = _Span(self, name, metadata)
        try:
            yield s
        finally:
            s.end()


_shared_tracer = None


def get_tracer() -> Tracer:
    global _shared_tracer
    if _shared_tracer is None:
        from app.config import get_settings

        _shared_tracer = Tracer(get_settings())
    return _shared_tracer

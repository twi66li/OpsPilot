"""缓存/限流：优先 Redis，不可用时自动退化为进程内实现（接口一致）。"""

import logging
import threading
import time

logger = logging.getLogger(__name__)


class InMemoryCache:
    def __init__(self):
        self._data: dict[str, tuple[str, float]] = {}
        self._counters: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if not item:
            return None
        value, expire_at = item
        if expire_at and time.time() > expire_at:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: str, ttl: int = 300) -> None:
        with self._lock:
            if ttl <= 0:  # 与 Redis 语义对齐：ttl=0 立即删除
                self._data.pop(key, None)
                return
            self._data[key] = (value, time.time() + ttl)

    def incr_with_ttl(self, key: str, ttl: int) -> int:
        with self._lock:
            count, reset_at = self._counters.get(key, (0, 0))
            now = time.time()
            if not reset_at or now > reset_at:
                count, reset_at = 0, now + ttl
            count += 1
            self._counters[key] = (count, reset_at)
            return count


class RedisCache:
    def __init__(self, url: str):
        import redis

        self.client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
        self.client.ping()

    def get(self, key: str) -> str | None:
        return self.client.get(key)

    def set(self, key: str, value: str, ttl: int = 300) -> None:
        self.client.set(key, value, ex=ttl)

    def incr_with_ttl(self, key: str, ttl: int) -> int:
        pipe = self.client.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl, nx=True)
        count, _ = pipe.execute()
        return int(count)


_cache_instance = None


def get_cache(settings=None):
    """单例缓存：Redis 不可达时退化为进程内；model_strict 下直接抛出（T7：真实后端不得被静默掩盖）。"""
    global _cache_instance
    if _cache_instance is None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        try:
            _cache_instance = RedisCache(settings.redis_url)
            logger.info("Redis 缓存已连接")
        except Exception as exc:
            if getattr(settings, "model_strict", False) or getattr(settings, "app_env", "development") == "production":
                raise RuntimeError(f"model_strict 模式下 Redis 不可达: {exc}") from exc
            logger.warning("Redis 不可达(%s)，使用进程内缓存", exc)
            _cache_instance = InMemoryCache()
    return _cache_instance


def check_rate_limit(cache, key: str, limit_per_minute: int) -> bool:
    """滑动窗口限流，返回是否放行。"""
    window = int(time.time()) // 60
    count = cache.incr_with_ttl(f"rl:{key}:{window}", ttl=120)
    return count <= limit_per_minute

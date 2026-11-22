"""缓存与限流单元测试。"""
from app.services.cache import InMemoryCache, check_rate_limit


def test_cache_set_get():
    cache = InMemoryCache()
    cache.set("k1", "v1", ttl=60)
    assert cache.get("k1") == "v1"
    assert cache.get("missing") is None


def test_cache_expiry():
    cache = InMemoryCache()
    cache.set("k", "v", ttl=0)  # 立即过期
    assert cache.get("k") is None


def test_counter_incr():
    cache = InMemoryCache()
    assert cache.incr_with_ttl("c", ttl=60) == 1
    assert cache.incr_with_ttl("c", ttl=60) == 2
    assert cache.incr_with_ttl("other", ttl=60) == 1


def test_rate_limit():
    cache = InMemoryCache()
    key = "tenant:user"
    assert check_rate_limit(cache, key, limit_per_minute=2)
    assert check_rate_limit(cache, key, limit_per_minute=2)
    assert not check_rate_limit(cache, key, limit_per_minute=2)

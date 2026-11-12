"""真实基础设施最小集成；不调用模型供应商，只清理本次生成的测试资源。"""

import uuid

import psycopg
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from sqlalchemy import text

from app.config import get_settings
from app.db.base import engine, init_db
from app.rag.vector_store import QdrantVectorStore
from app.services.cache import RedisCache


def main():
    settings = get_settings()
    if not settings.database_url.startswith("postgresql") or settings.qdrant_mode != "remote":
        raise RuntimeError("集成检查必须显式连接PostgreSQL、远端Qdrant及Redis")
    init_db()
    token = uuid.uuid4().hex
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0003"
        conn.execute(
            text("INSERT INTO tenants (id,name,created_at) VALUES (:id,'integration',CURRENT_TIMESTAMP)"), {"id": token}
        )
        assert conn.scalar(text("SELECT name FROM tenants WHERE id=:id"), {"id": token}) == "integration"
        conn.rollback()
    url = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as connection:
        saver = PostgresSaver(connection)
        saver.setup()
        thread_id = "integration-" + token
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"marker": token}
        checkpoint["channel_versions"] = {"marker": "1"}
        try:
            saved = saver.put(config, checkpoint, {"source": "input", "step": 0, "parents": {}}, {"marker": "1"})
            assert saver.get_tuple(saved).checkpoint["channel_values"]["marker"] == token
        finally:
            saver.delete_thread(thread_id)
    collection = "integration_" + token
    store = QdrantVectorStore(mode="remote", url=settings.qdrant_url, collection=collection, dim=4)
    try:
        store.upsert("tenant-a", [{"chunk_id": "a", "doc_id": "a", "doc_title": "A", "text": "owned"}], [[1, 0, 0, 0]])
        store.upsert("tenant-b", [{"chunk_id": "b", "doc_id": "b", "doc_title": "B", "text": "other"}], [[1, 0, 0, 0]])
        found = store.search("tenant-a", [1, 0, 0, 0])
        assert len(found) == 1 and found[0].doc_id == "a"
    finally:
        store.client.delete_collection(collection)
    cache = RedisCache(settings.redis_url)
    key = "integration:" + token
    try:
        assert cache.incr_with_ttl(key, 30) == 1 and cache.incr_with_ttl(key, 30) == 2
        assert cache.client.ttl(key) > 0
    finally:
        cache.client.delete(key)
    print("真实基础设施通过：数据库迁移/事务、检查点、向量隔离、Redis计数与过期；未调用模型")


if __name__ == "__main__":
    main()

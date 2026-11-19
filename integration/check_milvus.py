"""只写显式指定的新测试集合，使用固定向量对照 Qdrant 与真实 Milvus；不调用模型。"""

import argparse
import json
import os
from pathlib import Path

from pymilvus import MilvusClient

from app.rag.milvus_store import MilvusVectorStore
from app.rag.vector_store import QdrantVectorStore
from scripts.migrate_milvus import compare_dense, migrate


def check(uri: str, collection: str, token: str = "", db_name: str = "default") -> dict:
    if not collection.startswith("opspilot_test_"):
        raise ValueError("仅允许新建 opspilot_test_ 前缀的隔离测试集合")
    client = MilvusClient(uri=uri, token=token, db_name=db_name, timeout=15)
    try:
        if client.has_collection(collection_name=collection) or client.has_collection(collection_name=collection + "_migration"):
            raise ValueError("测试集合已存在，请指定新的名称，避免覆盖已有数据")
        version = client.get_server_version()
    finally:
        client.close()
    target = MilvusVectorStore(uri, collection, 1024, token, db_name)
    source = QdrantVectorStore(mode="memory", dim=1024)
    datasets = [
        ("official", "guide", "guide-v1", 0),
        ("personal:alice", "chosen", "chosen-v1", 1),
        ("personal:alice", "chosen", "chosen-v2", 2),
        ("personal:alice", "unselected", "other-v1", 0),
        ("personal:bob", "private", "private-v1", 0),
        ("personal:alice", "legacy", None, 3),
        ('quoted" or tenant_id != "quoted', "quoted", None, 4),
    ]
    for tenant, doc, version_id, index in datasets:
        chunk = {"chunk_id": f"c-{index}-{doc}-{version_id}", "doc_id": doc, "doc_title": doc, "text": "固定合成测试资料",
                 "source": "integration", "version_id": version_id, "page_start": 2, "heading_path": ["测试"],
                 "custom": {"retained": True}}
        vector = [0.0] * 1024
        vector[index] = 1.0
        for store in (source, target):
            store.upsert(tenant, [chunk], [vector])
            store.upsert(tenant, [chunk], [vector])
    assert source.count() == target.count() == len(datasets)
    comparisons = 0
    queries = []
    for tenant, versions in [("official", {"guide": "guide-v1"}),
                             ("personal:alice", {"chosen": "chosen-v2"}),
                             ("personal:alice", {"chosen": "chosen-v1"}),
                             ("personal:alice", {"legacy": None}),
                             ("personal:bob", {"private": "private-v1"}),
                             ('quoted" or tenant_id != "quoted', {"quoted": None})]:
        assert source.count(tenant) == target.count(tenant)
        assert sorted(source.scroll_tenant(tenant), key=lambda row: row["chunk_id"]) == sorted(
            target.scroll_tenant(tenant), key=lambda row: row["chunk_id"])
        query = [1.0] + [0.0] * 1023
        expected = source.search(tenant, query, top_k=10, active_versions=versions)
        actual = target.search(tenant, query, top_k=10, active_versions=versions)
        assert {hit.chunk_id for hit in actual} == {hit.chunk_id for hit in expected}
        assert {hit.doc_id for hit in actual} <= versions.keys()
        assert {hit.chunk_id: hit.to_dict() for hit in actual} == {hit.chunk_id: hit.to_dict() for hit in expected}
        assert target.search(tenant, query, active_versions={}) == []
        queries.append({"tenant_id": tenant, "query_vector": query, "top_k": 10, "active_versions": versions})
        comparisons += 1
    assert target.delete_doc("personal:alice", "unselected") == 1
    assert target.count("personal:bob") == 1 and target.count() == len(datasets) - 1
    assert target.health() == "ok"
    target.client.flush(collection_name=collection)
    target.client.close()
    target = MilvusVectorStore(uri, collection, 1024, token, db_name)
    assert target.count() == len(datasets) - 1
    target.client.close()
    migrated = MilvusVectorStore(uri, collection + "_migration", 1024, token, db_name)
    checkpoint = Path("data/integration") / (collection + ".json")
    migration = migrate(source.client, source.collection, migrated, checkpoint, collection + "_migration")
    resumed = migrate(source.client, source.collection, migrated, checkpoint, collection + "_migration")
    assert migration == resumed and migrated.count() == len(datasets)
    comparison = compare_dense(source, migrated, queries)
    assert comparison["different_queries"] == []
    migrated.client.close()
    source.client.close()
    return {"server_version": version, "collection": collection, "dim": 1024, "scope_comparisons": comparisons,
            "upsert_count_delete_payload": "passed", "reconnect": "passed", "server_restart": "not_run",
            "data": "synthetic_only", "collection_retained": True, "migration": migration,
            "resume": "passed", "dense_comparison": comparison}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--db-name", default="default")
    args = parser.parse_args()
    print(json.dumps(check(args.uri, args.collection, os.environ.get("MILVUS_TOKEN", ""), args.db_name), ensure_ascii=False))

"""Milvus 稠密存储：保留原 payload 与资料域，使用精确余弦检索。"""

import json

from pymilvus import DataType, MilvusClient

from app.rag.vector_store import RetrievedChunk, _location


def _literal(value: str) -> str:
    # 过滤值只作为 JSON 字符串常量，不能成为 Milvus 表达式。
    return json.dumps(value, ensure_ascii=False)


def _scope(tenant_id: str, active_versions: dict | None = None) -> str:
    scope = f"tenant_id == {_literal(tenant_id)}"
    if active_versions is not None:
        versions = " or ".join(
            f"(doc_id == {_literal(doc_id)} and version_id == {_literal(version or '')})"
            for doc_id, version in active_versions.items()
        )
        scope += f" and ({versions})"
    return scope


class MilvusVectorStore:
    def __init__(self, uri: str, collection: str, dim: int, token: str = "", db_name: str = "default"):
        self.collection, self.dim = collection, dim
        self.client = MilvusClient(uri=uri, token=token, db_name=db_name, timeout=15)
        if not self.client.has_collection(collection_name=collection):
            schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
            for name in ("tenant_id", "doc_id", "version_id"):
                schema.add_field(name, DataType.VARCHAR, max_length=128)
            schema.add_field("payload", DataType.JSON)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
            indexes = MilvusClient.prepare_index_params()
            indexes.add_index(field_name="vector", index_name="vector", index_type="FLAT", metric_type="COSINE")
            self.client.create_collection(
                collection_name=collection, schema=schema, index_params=indexes, consistency_level="Strong",
            )
        fields = {field["name"]: field for field in self.client.describe_collection(collection_name=collection)["fields"]}
        expected = {"chunk_id": DataType.VARCHAR, "tenant_id": DataType.VARCHAR, "doc_id": DataType.VARCHAR,
                    "version_id": DataType.VARCHAR, "payload": DataType.JSON, "vector": DataType.FLOAT_VECTOR}
        if any(name not in fields or fields[name]["type"] != dtype for name, dtype in expected.items()):
            raise ValueError("Milvus collection schema 与 OpsPilot 不匹配")
        if not fields["chunk_id"].get("is_primary") or int(fields["vector"]["params"]["dim"]) != dim:
            raise ValueError("Milvus 主键或向量维度不匹配")
        index = self.client.describe_index(collection_name=collection, index_name="vector")
        if index.get("metric_type") != "COSINE":
            raise ValueError("Milvus 向量索引必须使用 COSINE")
        self.client.load_collection(collection_name=collection)

    def upsert(self, tenant_id: str, chunks: list[dict], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors) or any(len(vector) != self.dim for vector in vectors):
            raise ValueError("分块数量或向量维度不匹配")
        if not chunks:
            return
        rows = [{"chunk_id": chunk["chunk_id"], "tenant_id": tenant_id, "doc_id": chunk["doc_id"],
                 "version_id": chunk.get("version_id") or "", "payload": {**chunk, "tenant_id": tenant_id},
                 "vector": vector} for chunk, vector in zip(chunks, vectors)]
        self.client.upsert(collection_name=self.collection, data=rows)

    def search(self, tenant_id: str, query_vector: list[float], top_k: int = 5,
               active_versions: dict | None = None) -> list[RetrievedChunk]:
        if active_versions is not None and not active_versions:
            return []
        results = self.client.search(
            collection_name=self.collection, data=[query_vector], anns_field="vector", limit=top_k,
            filter=_scope(tenant_id, active_versions), output_fields=["payload"],
            search_params={"metric_type": "COSINE", "params": {}}, consistency_level="Strong",
        )
        chunks = []
        for hit in results[0]:
            payload = hit["entity"]["payload"]
            chunks.append(RetrievedChunk(
                chunk_id=payload["chunk_id"], doc_id=payload["doc_id"], doc_title=payload["doc_title"],
                text=payload["text"], source=payload.get("source", ""), score=float(hit["distance"]),
                tenant_id=tenant_id, extra=_location(payload),
            ))
        return chunks

    def delete_doc(self, tenant_id: str, doc_id: str) -> int:
        result = self.client.delete(
            collection_name=self.collection, filter=f"{_scope(tenant_id)} and doc_id == {_literal(doc_id)}",
        )
        return result["delete_count"]

    def count(self, tenant_id: str | None = None) -> int:
        rows = self.client.query(
            collection_name=self.collection, filter=_scope(tenant_id) if tenant_id is not None else "",
            output_fields=["count(*)"], consistency_level="Strong",
        )
        return int(rows[0]["count(*)"])

    def scroll_tenant(self, tenant_id: str) -> list[dict]:
        iterator = self.client.query_iterator(
            collection_name=self.collection, filter=_scope(tenant_id), output_fields=["payload"],
            batch_size=256, consistency_level="Strong",
        )
        chunks = []
        try:
            while batch := iterator.next():
                chunks.extend(dict(row["payload"]) for row in batch)
        finally:
            iterator.close()
        return chunks

    def health(self) -> str:
        # 查询而不是仅看 SDK 对象存在，确保 collection 已加载且实际可读。
        self.count()
        return "ok"

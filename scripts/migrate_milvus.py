"""Qdrant → Milvus 独立集合试迁移；默认只读盘点，--apply 才备份和写入。"""

import argparse
import hashlib
import json
import struct
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import httpx
from qdrant_client import QdrantClient

from app.config import Settings
from app.rag.milvus_store import MilvusVectorStore
from app.rag.vector_store import QdrantVectorStore


def batches(client, collection, offset=None, batch_size=128):
    while True:
        points, following = client.scroll(collection, offset=offset, limit=batch_size,
                                           with_payload=True, with_vectors=True)
        rows = []
        for point in points:
            if not isinstance(point.vector, list) or not point.payload or not point.payload.get("tenant_id"):
                raise ValueError("源分块缺少单一稠密向量或资料域，停止迁移")
            rows.append({"payload": dict(point.payload), "vector": point.vector})
        yield rows, following
        if following is None:
            return
        offset = following


def row_digest(row):
    payload = json.dumps(row["payload"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    # 两个 SDK 均传输 float32；避免 Python 的浮点字符串格式差异影响校验。
    vector = struct.pack(f"<{len(row['vector'])}f", *row["vector"])
    return hashlib.sha256(payload + b"\0" + vector).hexdigest()


def fingerprint(rows):
    entries = {}
    for row in rows:
        key = row["payload"]["chunk_id"]
        if key in entries:
            raise ValueError("源索引有重复逻辑 chunk_id，不能幂等迁移")
        entries[key] = row_digest(row)
    return {"count": len(entries), "sha256": hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def source_rows(client, collection):
    for rows, _ in batches(client, collection):
        yield from rows


def target_rows(target):
    iterator = target.client.query_iterator(collection_name=target.collection, output_fields=["payload", "vector"],
                                            batch_size=128, consistency_level="Strong")
    try:
        while rows := iterator.next():
            yield from rows
    finally:
        iterator.close()


def save_checkpoint(checkpoint: Path, state: dict):
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    temporary = checkpoint.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(checkpoint)


def migrate(source, collection, target, checkpoint: Path, target_identity: str, backup: dict | None = None):
    source_fingerprint = fingerprint(source_rows(source, collection))
    signature = {"source_collection": collection, "source": source_fingerprint,
                 "target_identity": target_identity, "target_collection": target.collection, "dim": target.dim}
    state = {"signature": signature, "offset": None, "copied": 0, "complete": False, "backup": backup}
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        if state["signature"] != signature:
            raise ValueError("源数据、目标或维度已变化，不能复用断点")
    elif target.count() != 0:
        raise ValueError("无对应断点且目标不为空，拒绝覆盖已有集合")
    if not state["complete"]:
        if state["offset"] is None:
            state["copied"] = 0
        for rows, offset in batches(source, collection, state["offset"]):
            grouped = defaultdict(list)
            for row in rows:
                grouped[row["payload"]["tenant_id"]].append(row)
            for tenant, group in grouped.items():
                target.upsert(tenant, [row["payload"] for row in group], [row["vector"] for row in group])
            if rows:
                actual = target.client.get(collection_name=target.collection,
                                           ids=[row["payload"]["chunk_id"] for row in rows],
                                           output_fields=["payload", "vector"], consistency_level="Strong")
                if fingerprint(actual) != fingerprint(rows):
                    raise ValueError("写入后的向量或完整 payload 校验失败")
            state.update(offset=offset, copied=state["copied"] + len(rows))
            save_checkpoint(checkpoint, state)
    target.client.flush(collection_name=target.collection)
    if fingerprint(source_rows(source, collection)) != source_fingerprint:
        raise ValueError("迁移期间源发生变化，需重新安排入库暂停窗口")
    if fingerprint(target_rows(target)) != source_fingerprint:
        raise ValueError("迁移完成后全量内容校验失败")
    state["complete"] = True
    save_checkpoint(checkpoint, state)
    return {"source": source_fingerprint, "target_collection": target.collection, "verified": True}


def compare_dense(source, target, queries):
    differences = []
    for index, query in enumerate(queries):
        expected = {hit.chunk_id: hit for hit in source.search(**query)}
        actual = {hit.chunk_id: hit for hit in target.search(**query)}
        if expected.keys() != actual.keys() or any(abs(hit.score - actual[key].score) > 1e-5
                                                   for key, hit in expected.items() if key in actual):
            differences.append(index)
    return {"queries": len(queries), "different_queries": differences}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-uri", required=True)
    parser.add_argument("--target-collection", required=True)
    parser.add_argument("--db-name", default="default")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="源入库须暂停；先创建并下载源快照，再写入专用目标")
    args = parser.parse_args()
    settings = Settings()
    source = QdrantClient(url=settings.qdrant_url, timeout=30)
    collection = settings.qdrant_collection
    info = source.get_collection(collection)
    vectors = info.config.params.vectors
    if isinstance(vectors, dict) or vectors.size != settings.embedding_dim or vectors.distance.value != "Cosine":
        raise ValueError("源向量类型、维度或距离不匹配")
    inventory = {"source_collection": collection, "dim": vectors.size, "configured_embedding_model": settings.embedding_model,
                 "source": fingerprint(source_rows(source, collection)), "apply": args.apply}
    if not args.apply:
        source.close()
        print(json.dumps(inventory, ensure_ascii=False))
        return
    backup = None
    if not args.checkpoint.exists():
        snapshot = source.create_snapshot(collection_name=collection)
        backup_path = args.checkpoint.parent / snapshot.name
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        url = settings.qdrant_url.rstrip("/") + f"/collections/{quote(collection, safe='')}/snapshots/{quote(snapshot.name, safe='')}"
        with httpx.stream("GET", url, timeout=120) as response:
            response.raise_for_status()
            with backup_path.open("wb") as output:
                for piece in response.iter_bytes():
                    output.write(piece)
        backup = {"snapshot": snapshot.name, "local_file": str(backup_path)}
    target = MilvusVectorStore(args.target_uri, args.target_collection, settings.embedding_dim,
                               settings.milvus_token, args.db_name)
    try:
        identity = hashlib.sha256(f"{args.target_uri}|{args.db_name}|{args.target_collection}".encode()).hexdigest()
        result = migrate(source, collection, target, args.checkpoint, identity, backup)
        # 固定源向量作查询，不重新嵌入；只对比同一资料域及同一文档版本。
        queries = []
        for row in source_rows(source, collection):
            p = row["payload"]
            queries.append({"tenant_id": p["tenant_id"], "query_vector": row["vector"], "top_k": 5,
                            "active_versions": {p["doc_id"]: p.get("version_id")}})
            if len(queries) == 16:
                break
        source_store = object.__new__(QdrantVectorStore)
        source_store.client, source_store.collection = source, collection
        result["comparison"] = compare_dense(source_store, target, queries)
        if result["comparison"]["different_queries"]:
            raise ValueError("稠密召回存在差异，保留源索引与目标供复核，不切换运行配置")
        print(json.dumps(result, ensure_ascii=False))
    finally:
        target.client.close()
        source.close()


if __name__ == "__main__":
    main()

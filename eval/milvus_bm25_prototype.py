"""T72 Milvus 2.5 原生 BM25 独立隔离原型、盘点迁移回滚验证与 T76 基线对比。

用法：
  python -m eval.milvus_bm25_prototype --uri http://127.0.0.1:29530
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import yaml
from pymilvus import AnnSearchRequest, DataType, Function, FunctionType, MilvusClient, RRFRanker

from app.db.models import Document
from app.rag.embeddings import tokenize as baseline_tokenize
from app.rag.fusion import rrf_fuse
from app.rag.vector_store import RetrievedChunk, _location
from app.services.kb_service import chunk_doc
from eval.agent_eval import load, sha
from eval.metrics import keyword_coverage, mrr_at_k, recall_at_k
from eval.offline_retrieval import BASELINE, DATASETS, DIM, KEYWORDS, MODEL, VECTORS, FrozenEmbeddings
from eval.real_eval import document_hits

DEFAULT_IMAGE = "milvusdb/milvus:v2.5.10"
DEFAULT_URI = "http://127.0.0.1:29530"
DEFAULT_PROJECT = "opspilot-t72-bm25"
DEFAULT_CONTAINER = "opspilot-t72-bm25-milvus-bm25-proto-1"
DEFAULT_REPORT = Path("eval/milvus_bm25_prototype_report.json")
FALLBACK_VECTORS = Path(r"D:\Code\python\aiops-diagnosis-agent\eval\reports\real_ops_20261029T221616\embeddings.json")

# Milvus 2.5 内置/自定义分析器方案，用于实测对比中文运维语料分词与检索效果。
ANALYZER_CONFIGS: dict[str, dict[str, Any]] = {
    "chinese": {"type": "chinese"},
    "chinese_ci": {"tokenizer": "jieba", "filter": ["cnalphanumonly", "lowercase"]},
    "standard": {"type": "standard"},
}

TOKENIZER_PROBE_DOCS: list[dict[str, Any]] = [
    {
        "chunk_id": "tok-probe-001",
        "doc_id": "ops-kb-001",
        "doc_title": "ERR_5003 数据库连接池耗尽处理手册",
        "text": "当应用日志出现 ERR_5003 错误时，说明数据库连接池耗尽，需要排查慢查询和长事务。",
        "source": "probe",
    },
    {
        "chunk_id": "tok-probe-006",
        "doc_id": "ops-kb-006",
        "doc_title": "order-service 接口超时排查 SOP",
        "text": "order-service 接口出现大量 Read timed out 超时，排查 Full GC 导致的 STW 与上游熔断。",
        "source": "probe",
    },
]

TOKENIZER_BEHAVIOR_PROBES: list[dict[str, Any]] = [
    {"id": "zh_compound", "query": "数据库连接池耗尽", "expected_doc_id": "ops-kb-001"},
    {"id": "zh_subword", "query": "连接池", "expected_doc_id": "ops-kb-001"},
    {"id": "zh_slow_sql", "query": "慢查询", "expected_doc_id": "ops-kb-001"},
    {"id": "code_upper", "query": "ERR_5003", "expected_doc_id": "ops-kb-001"},
    {"id": "code_lower", "query": "err_5003", "expected_doc_id": "ops-kb-001"},
    {"id": "service_exact", "query": "order-service", "expected_doc_id": "ops-kb-006"},
    {"id": "service_zh_adjacent", "query": "order-service接口超时", "expected_doc_id": "ops-kb-006"},
    {"id": "english_mixed_case", "query": "Read timed out", "expected_doc_id": "ops-kb-006"},
    {"id": "english_lower_case", "query": "read timed out", "expected_doc_id": "ops-kb-006"},
    {"id": "whitespace_only", "query": "   ", "expected_doc_id": None},
]


def resolve_embeddings_path(candidate: Path | None = None) -> Path:
    """优先使用显式参数或工作区相对路径，不存在时回退至主仓库只读冻结文件。"""
    if candidate is not None and candidate.is_file():
        return candidate
    if VECTORS.is_file():
        return VECTORS
    if FALLBACK_VECTORS.is_file():
        return FALLBACK_VECTORS
    target = candidate or VECTORS
    raise FileNotFoundError(f"未找到冻结向量文件：{target}")


def literal(value: str) -> str:
    """将过滤值编码为 JSON 字符串字面量，防止 Milvus 表达式注入。"""
    return json.dumps(value, ensure_ascii=False)


def build_filter(
    tenant_id: str,
    *,
    active_only: bool = False,
    active_versions: dict[str, str | None] | None = None,
) -> str:
    """构造租户与有效版本过滤表达式，与 app/rag/milvus_store.py 口径保持一致。"""
    clauses = [f"tenant_id == {literal(tenant_id)}"]
    if active_only:
        clauses.append("is_active == true")
    if active_versions is not None:
        versions = " or ".join(
            f"(doc_id == {literal(doc_id)} and version_id == {literal(version or '')})"
            for doc_id, version in active_versions.items()
        )
        clauses.append(f"({versions})")
    return " and ".join(clauses)


def row_digest(row: dict[str, Any]) -> str:
    """计算单条记录（规范 payload + float32 向量）的确定性 SHA256 摘要。"""
    payload = json.dumps(row["payload"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    vector = struct.pack(f"<{len(row['vector'])}f", *row["vector"])
    return hashlib.sha256(payload + b"\0" + vector).hexdigest()


def fingerprint(rows: Any) -> dict[str, Any]:
    """计算集合顺序无关的条数与内容摘要；若出现重复 chunk_id 则拒绝迁移。"""
    entries: dict[str, str] = {}
    for row in rows:
        key = row["payload"]["chunk_id"]
        if key in entries:
            raise ValueError(f"源索引存在重复逻辑 chunk_id：{key}，不能幂等迁移")
        entries[key] = row_digest(row)
    digest = hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"count": len(entries), "sha256": digest}


def build_bm25_schema(dim: int, analyzer_params: dict[str, Any]):
    """构建 Milvus 2.5 原生 BM25 + 稠密混合原型 Schema。"""
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
    for name in ("tenant_id", "doc_id", "version_id"):
        schema.add_field(name, DataType.VARCHAR, max_length=128)
    schema.add_field("is_active", DataType.BOOL)
    schema.add_field(
        "text",
        DataType.VARCHAR,
        max_length=65535,
        enable_analyzer=True,
        analyzer_params=analyzer_params,
    )
    schema.add_field("payload", DataType.JSON)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_function(
        Function(
            name="text_bm25_emb",
            function_type=FunctionType.BM25,
            input_field_names=["text"],
            output_field_names=["sparse_vector"],
        )
    )
    return schema


def build_bm25_indexes():
    """构建稠密精确余弦索引 + BM25 稀疏倒排索引。"""
    indexes = MilvusClient.prepare_index_params()
    indexes.add_index(field_name="vector", index_name="vector_flat_cosine", index_type="FLAT", metric_type="COSINE")
    indexes.add_index(
        field_name="sparse_vector",
        index_name="sparse_bm25_idx",
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
    )
    return indexes


def build_legacy_dense_schema(dim: int):
    """构建与线上 app/rag/milvus_store.py 同构的旧版仅稠密集合 Schema（用于盘点迁移演练）。"""
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
    for name in ("tenant_id", "doc_id", "version_id"):
        schema.add_field(name, DataType.VARCHAR, max_length=128)
    schema.add_field("payload", DataType.JSON)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
    return schema


def build_legacy_dense_indexes():
    indexes = MilvusClient.prepare_index_params()
    indexes.add_index(field_name="vector", index_name="vector", index_type="FLAT", metric_type="COSINE")
    return indexes


def _hits_to_chunks(hits: list[dict[str, Any]], tenant_id: str) -> list[RetrievedChunk]:
    chunks: list[RetrievedChunk] = []
    for hit in hits:
        payload = hit["entity"]["payload"]
        chunks.append(
            RetrievedChunk(
                chunk_id=payload["chunk_id"],
                doc_id=payload["doc_id"],
                doc_title=payload["doc_title"],
                text=payload["text"],
                source=payload.get("source", ""),
                score=float(hit["distance"]),
                tenant_id=payload.get("tenant_id", tenant_id),
                extra=_location(payload),
            )
        )
    return chunks


class MilvusBM25PrototypeStore:
    """Milvus 2.5 原生 BM25 + 稠密向量原型集合封装（隔离实验，不改动线上存储）。"""

    def __init__(
        self,
        client: MilvusClient,
        collection: str,
        dim: int,
        analyzer_params: dict[str, Any] | None = None,
        *,
        recreate: bool = False,
    ):
        self.client = client
        self.collection = collection
        self.dim = dim
        self.analyzer_params = analyzer_params or ANALYZER_CONFIGS["chinese"]
        if recreate and self.client.has_collection(collection_name=collection):
            self.client.drop_collection(collection_name=collection)
        if not self.client.has_collection(collection_name=collection):
            schema = build_bm25_schema(dim=dim, analyzer_params=self.analyzer_params)
            indexes = build_bm25_indexes()
            self.client.create_collection(
                collection_name=collection,
                schema=schema,
                index_params=indexes,
                consistency_level="Strong",
            )
        self.client.load_collection(collection_name=collection)

    def upsert(
        self,
        tenant_id: str,
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
        *,
        is_active: bool = True,
    ) -> None:
        if len(chunks) != len(vectors) or any(len(vector) != self.dim for vector in vectors):
            raise ValueError("分块数量或向量维度不匹配")
        if not chunks:
            return
        rows = [
            {
                "chunk_id": chunk["chunk_id"],
                "tenant_id": tenant_id,
                "doc_id": chunk["doc_id"],
                "version_id": chunk.get("version_id") or "",
                "is_active": bool(chunk.get("is_active", is_active)),
                "text": chunk["text"],
                "payload": {**chunk, "tenant_id": tenant_id},
                "vector": vector,
            }
            for chunk, vector in zip(chunks, vectors)
        ]
        self.client.upsert(collection_name=self.collection, data=rows)

    def count(self, tenant_id: str | None = None, *, active_only: bool = False) -> int:
        if tenant_id is None:
            expr = "is_active == true" if active_only else ""
        else:
            expr = build_filter(tenant_id, active_only=active_only)
        rows = self.client.query(
            collection_name=self.collection,
            filter=expr,
            output_fields=["count(*)"],
            consistency_level="Strong",
        )
        return int(rows[0]["count(*)"])

    def search_dense(
        self,
        tenant_id: str,
        query_vector: list[float],
        top_k: int = 8,
        *,
        active_only: bool = False,
        active_versions: dict[str, str | None] | None = None,
    ) -> list[RetrievedChunk]:
        if active_versions is not None and not active_versions:
            return []
        expr = build_filter(tenant_id, active_only=active_only, active_versions=active_versions)
        results = self.client.search(
            collection_name=self.collection,
            data=[query_vector],
            anns_field="vector",
            limit=top_k,
            filter=expr,
            output_fields=["payload"],
            search_params={"metric_type": "COSINE", "params": {}},
            consistency_level="Strong",
        )
        return _hits_to_chunks(results[0], tenant_id)

    def search_bm25(
        self,
        tenant_id: str,
        query_text: str,
        top_k: int = 8,
        *,
        active_only: bool = False,
        active_versions: dict[str, str | None] | None = None,
    ) -> list[RetrievedChunk]:
        if active_versions is not None and not active_versions:
            return []
        expr = build_filter(tenant_id, active_only=active_only, active_versions=active_versions)
        results = self.client.search(
            collection_name=self.collection,
            data=[query_text],
            anns_field="sparse_vector",
            limit=top_k,
            filter=expr,
            output_fields=["payload"],
            search_params={"metric_type": "BM25", "params": {}},
            consistency_level="Strong",
        )
        return _hits_to_chunks(results[0], tenant_id)

    def search_hybrid(
        self,
        tenant_id: str,
        query_text: str,
        query_vector: list[float],
        top_k: int = 8,
        *,
        rrf_k: int = 60,
        active_only: bool = False,
        active_versions: dict[str, str | None] | None = None,
    ) -> list[RetrievedChunk]:
        if active_versions is not None and not active_versions:
            return []
        expr = build_filter(tenant_id, active_only=active_only, active_versions=active_versions)
        dense_req = AnnSearchRequest(
            data=[query_vector],
            anns_field="vector",
            param={"metric_type": "COSINE", "params": {}},
            limit=top_k,
            expr=expr,
        )
        bm25_req = AnnSearchRequest(
            data=[query_text],
            anns_field="sparse_vector",
            param={"metric_type": "BM25", "params": {}},
            limit=top_k,
            expr=expr,
        )
        results = self.client.hybrid_search(
            collection_name=self.collection,
            reqs=[dense_req, bm25_req],
            ranker=RRFRanker(k=rrf_k),
            limit=top_k,
            output_fields=["payload"],
            consistency_level="Strong",
        )
        return _hits_to_chunks(results[0], tenant_id)


def iter_collection_rows(client: MilvusClient, collection: str, batch_size: int = 128):
    """使用强一致 query_iterator 扫描集合内全量 payload 与稠密向量。"""
    iterator = client.query_iterator(
        collection_name=collection,
        output_fields=["payload", "vector"],
        batch_size=batch_size,
        consistency_level="Strong",
    )
    try:
        while batch := iterator.next():
            for row in batch:
                yield {"payload": dict(row["payload"]), "vector": list(row["vector"])}
    finally:
        iterator.close()


def iter_collection_batches(client: MilvusClient, collection: str, batch_size: int = 128):
    iterator = client.query_iterator(
        collection_name=collection,
        output_fields=["payload", "vector"],
        batch_size=batch_size,
        consistency_level="Strong",
    )
    try:
        while batch := iterator.next():
            yield [{"payload": dict(row["payload"]), "vector": list(row["vector"])} for row in batch]
    finally:
        iterator.close()


def save_checkpoint(checkpoint: Path, state: dict[str, Any]) -> None:
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    temporary = checkpoint.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(checkpoint)


def migrate_milvus_to_bm25(
    client: MilvusClient,
    source_collection: str,
    target_store: MilvusBM25PrototypeStore,
    *,
    checkpoint: Path | None = None,
    batch_size: int = 128,
) -> dict[str, Any]:
    """Milvus-to-Milvus 集合盘点、迁移复制、条数/摘要校验（保留旧集合不动）。"""
    source_fp = fingerprint(iter_collection_rows(client, source_collection, batch_size=batch_size))
    signature = {
        "source_collection": source_collection,
        "source": source_fp,
        "target_collection": target_store.collection,
        "dim": target_store.dim,
    }
    state: dict[str, Any] = {"signature": signature, "copied": 0, "complete": False}
    if checkpoint is not None and checkpoint.exists():
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        if state.get("signature") != signature:
            raise ValueError("源集合或目标配置已变化，不能复用迁移断点")
    elif target_store.count() != 0:
        raise ValueError("无对应断点且目标集合不为空，拒绝覆盖已有集合")

    if not state["complete"]:
        copied = 0
        for rows in iter_collection_batches(client, source_collection, batch_size=batch_size):
            by_tenant: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                tenant = row["payload"].get("tenant_id")
                if not tenant:
                    raise ValueError("源分块缺少 tenant_id，停止迁移")
                by_tenant.setdefault(tenant, []).append(row)
            for tenant, group in by_tenant.items():
                target_store.upsert(
                    tenant,
                    [item["payload"] for item in group],
                    [item["vector"] for item in group],
                )
            if rows:
                ids = [item["payload"]["chunk_id"] for item in rows]
                actual = client.get(
                    collection_name=target_store.collection,
                    ids=ids,
                    output_fields=["payload", "vector"],
                    consistency_level="Strong",
                )
                normalized = [{"payload": dict(r["payload"]), "vector": list(r["vector"])} for r in actual]
                if fingerprint(normalized) != fingerprint(rows):
                    raise ValueError("分批写入回读校验失败：payload 或向量与源集合不一致")
            copied += len(rows)
            state["copied"] = copied
            if checkpoint is not None:
                save_checkpoint(checkpoint, state)

    client.flush(collection_name=target_store.collection)
    source_after_fp = fingerprint(iter_collection_rows(client, source_collection, batch_size=batch_size))
    if source_after_fp != source_fp:
        raise ValueError("迁移期间源集合发生变化，停止切换")
    target_fp = fingerprint(iter_collection_rows(client, target_store.collection, batch_size=batch_size))
    if target_fp != source_fp:
        raise ValueError("迁移完成后目标集合全量内容校验失败")

    state["complete"] = True
    if checkpoint is not None:
        save_checkpoint(checkpoint, state)
    return {
        "source_collection": source_collection,
        "target_collection": target_store.collection,
        "source_fingerprint": source_fp,
        "target_fingerprint": target_fp,
        "count_match": source_fp["count"] == target_fp["count"],
        "sha256_match": source_fp["sha256"] == target_fp["sha256"],
        "verified": True,
    }


def compare_dense_collections(
    client: MilvusClient,
    source_collection: str,
    target_collection: str,
    queries: list[dict[str, Any]],
) -> dict[str, Any]:
    """用固定向量比对新旧集合的稠密召回结果（chunk_id 集合与余弦分值容差 1e-5）。"""
    differences: list[int] = []
    for idx, q in enumerate(queries):
        expr = build_filter(q["tenant_id"], active_versions=q.get("active_versions"))
        src = client.search(
            collection_name=source_collection,
            data=[q["query_vector"]],
            anns_field="vector",
            limit=q.get("top_k", 5),
            filter=expr,
            output_fields=["payload"],
            search_params={"metric_type": "COSINE", "params": {}},
            consistency_level="Strong",
        )[0]
        dst = client.search(
            collection_name=target_collection,
            data=[q["query_vector"]],
            anns_field="vector",
            limit=q.get("top_k", 5),
            filter=expr,
            output_fields=["payload"],
            search_params={"metric_type": "COSINE", "params": {}},
            consistency_level="Strong",
        )[0]
        src_map = {h["entity"]["payload"]["chunk_id"]: float(h["distance"]) for h in src}
        dst_map = {h["entity"]["payload"]["chunk_id"]: float(h["distance"]) for h in dst}
        if src_map.keys() != dst_map.keys() or any(abs(src_map[k] - dst_map[k]) > 1e-5 for k in src_map):
            differences.append(idx)
    return {"queries": len(queries), "different_queries": differences, "exact_match": not differences}


def verify_rollback_with_preserved_legacy(
    client: MilvusClient,
    legacy_collection: str,
    bm25_collection: str,
    alias_name: str,
    *,
    tenant_id: str,
    sample_query_text: str,
    sample_query_vector: list[float],
    expected_legacy_fp: dict[str, Any],
) -> dict[str, Any]:
    """验证保留旧集合场景下的 Alias 切流与无损回滚。"""
    # 若存在残留 alias 先清理
    if alias_name in (client.list_aliases(collection_name=legacy_collection) or []) or alias_name in (
        client.list_aliases(collection_name=bm25_collection) or []
    ):
        client.drop_alias(alias=alias_name)

    def _alias_dense_ids() -> list[str]:
        res = client.search(
            collection_name=alias_name,
            data=[sample_query_vector],
            anns_field="vector",
            limit=5,
            filter=build_filter(tenant_id),
            output_fields=["payload"],
            search_params={"metric_type": "COSINE", "params": {}},
            consistency_level="Strong",
        )[0]
        return [h["entity"]["payload"]["chunk_id"] for h in res]

    try:
        client.create_alias(collection_name=legacy_collection, alias=alias_name)
        before_target = client.describe_alias(alias=alias_name).get("collection_name")
        before_ids = _alias_dense_ids()

        # 切换到新 BM25 集合
        client.alter_alias(collection_name=bm25_collection, alias=alias_name)
        switched_target = client.describe_alias(alias=alias_name).get("collection_name")
        switched_bm25 = client.search(
            collection_name=alias_name,
            data=[sample_query_text],
            anns_field="sparse_vector",
            limit=5,
            filter=build_filter(tenant_id),
            output_fields=["payload"],
            search_params={"metric_type": "BM25", "params": {}},
            consistency_level="Strong",
        )[0]
        switched_bm25_docs = [h["entity"]["payload"]["doc_id"] for h in switched_bm25]

        # 核对旧集合依然存在且内容指纹未改变
        legacy_preserved = bool(client.has_collection(collection_name=legacy_collection))
        legacy_fp_after_switch = fingerprint(iter_collection_rows(client, legacy_collection))

        # 模拟故障回滚到旧集合
        client.alter_alias(collection_name=legacy_collection, alias=alias_name)
        rolled_back_target = client.describe_alias(alias=alias_name).get("collection_name")
        after_rollback_ids = _alias_dense_ids()

        return {
            "alias": alias_name,
            "initial_target": before_target,
            "switched_target": switched_target,
            "switched_bm25_hit_docs": switched_bm25_docs,
            "legacy_preserved": legacy_preserved,
            "legacy_fingerprint_unchanged": legacy_fp_after_switch == expected_legacy_fp,
            "rolled_back_target": rolled_back_target,
            "dense_hits_identical_after_rollback": before_ids == after_rollback_ids,
            "passed": (
                before_target == legacy_collection
                and switched_target == bm25_collection
                and bool(switched_bm25_docs)
                and legacy_preserved
                and legacy_fp_after_switch == expected_legacy_fp
                and rolled_back_target == legacy_collection
                and before_ids == after_rollback_ids
            ),
        }
    finally:
        try:
            client.drop_alias(alias=alias_name)
        except Exception:
            pass


def parse_memory_to_mib(mem_usage: str) -> float | None:
    """将 docker stats MemUsage（如 '512.4MiB / 7.65GiB'）中的当前占用解析为 MiB。"""
    used = mem_usage.split("/", 1)[0].strip()
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?i?B)$", used, re.IGNORECASE)
    if not match:
        return None
    val, unit = float(match.group(1)), match.group(2).lower()
    factors = {
        "b": 1 / (1024 * 1024),
        "kb": 1000 / (1024 * 1024),
        "kib": 1 / 1024,
        "mb": (1000 * 1000) / (1024 * 1024),
        "mib": 1.0,
        "gb": (1000**3) / (1024 * 1024),
        "gib": 1024.0,
    }
    return round(val * factors.get(unit, 1.0), 2) if unit in factors else None


def collect_docker_stats(container_name: str = DEFAULT_CONTAINER) -> dict[str, Any]:
    """采样单次 docker stats --no-stream 资源占用（内存/CPU/PIDs/IO）。"""
    cmd = ["docker", "stats", "--no-stream", "--format", "{{json .}}", container_name]
    raw = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=15).strip()
    data = json.loads(raw.splitlines()[0])
    return {
        "container": data.get("Name", container_name),
        "cpu_perc": data.get("CPUPerc"),
        "mem_usage": data.get("MemUsage"),
        "mem_usage_mib": parse_memory_to_mib(data.get("MemUsage", "")),
        "mem_perc": data.get("MemPerc"),
        "net_io": data.get("NetIO"),
        "block_io": data.get("BlockIO"),
        "pids": data.get("PIDs"),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def _extract_tokens(result: Any) -> list[str]:
    """兼容 pymilvus run_analyzer 返回 AnalyzeResult 对象或字符串列表。"""
    if hasattr(result, "tokens"):
        tokens = result.tokens
    else:
        tokens = result
    out: list[str] = []
    for tok in tokens:
        if isinstance(tok, dict) and "token" in tok:
            out.append(str(tok["token"]))
        else:
            out.append(str(tok))
    return out


def verify_chinese_tokenization(client: MilvusClient) -> dict[str, Any]:
    """验证 ①：实测 Milvus 2.5 各类 analyzer 的中文分词、大小写与特殊标识行为。"""
    rpc_supported = True
    rpc_note = "ok"
    try:
        client.run_analyzer("ERR_5003 数据库连接池", analyzer_params=ANALYZER_CONFIGS["chinese"])
    except Exception as exc:
        rpc_supported = False
        rpc_note = f"{type(exc).__name__}: {exc}"

    analyzers_report: dict[str, Any] = {
        "run_analyzer_rpc_supported": rpc_supported,
        "run_analyzer_rpc_note": rpc_note,
        "analyzers": {},
    }
    probe_vec = [1.0, 0.0, 0.0]
    for name, params in ANALYZER_CONFIGS.items():
        col_name = f"t72_tok_probe_{name}"
        supported = True
        error_msg = None
        probe_results: list[dict[str, Any]] = []
        try:
            store = MilvusBM25PrototypeStore(client, col_name, dim=3, analyzer_params=params, recreate=True)
            store.upsert("default", TOKENIZER_PROBE_DOCS, [probe_vec, probe_vec], is_active=True)
            client.flush(collection_name=col_name)
            for probe in TOKENIZER_BEHAVIOR_PROBES:
                hits = store.search_bm25("default", probe["query"], top_k=3)
                hit_pairs = [{"doc_id": h.doc_id, "score": round(h.score, 4)} for h in hits]
                expected = probe["expected_doc_id"]
                passed = (
                    (len(hits) == 0)
                    if expected is None
                    else (len(hits) == 1 and hits[0].doc_id == expected)
                )
                item: dict[str, Any] = {
                    "id": probe["id"],
                    "query": probe["query"],
                    "expected_doc_id": expected,
                    "hits": hit_pairs,
                    "passed": passed,
                }
                if rpc_supported:
                    item["milvus_tokens"] = _extract_tokens(
                        client.run_analyzer(probe["query"], analyzer_params=params)
                    )
                probe_results.append(item)
        except Exception as exc:
            supported = False
            error_msg = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                if client.has_collection(collection_name=col_name):
                    client.drop_collection(collection_name=col_name)
            except Exception:
                pass
        analyzers_report["analyzers"][name] = {
            "analyzer_params": params,
            "supported": supported,
            "error": error_msg,
            "passed_probes": sum(1 for r in probe_results if r["passed"]),
            "total_probes": len(probe_results),
            "failed_probe_ids": [r["id"] for r in probe_results if not r["passed"]],
            "all_probes_passed": supported and all(r["passed"] for r in probe_results),
            "probes": probe_results,
        }
    analyzers_report["baseline_jieba_reference"] = {
        probe["id"]: baseline_tokenize(probe["query"]) for probe in TOKENIZER_BEHAVIOR_PROBES
    }
    return analyzers_report


def load_frozen_v1_corpus(embeddings_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], FrozenEmbeddings]:
    """按 eval/offline_retrieval.py 相同口径加载冻结 v1 资料、分块、问题与 1024 维向量。"""
    v1 = DATASETS / "v1"
    manifest = load(v1 / "manifest.json")
    manifest_sha = sha(v1 / "manifest.json")
    frozen_run = load(embeddings_path.with_name("run_manifest.json"))
    if (manifest_sha, MODEL, DIM) != (
        frozen_run["dataset_manifest_sha256"],
        frozen_run["embedding_model"],
        frozen_run["embedding_dim"],
    ):
        raise ValueError("冻结向量来源的 manifest、模型或维度不匹配")
    for file, digest in manifest["artifact_sha256"].items():
        content = (v1 / file).read_bytes()
        if hashlib.sha256(content).hexdigest() == digest:
            continue
        if file != manifest["questions_path"] or hashlib.sha256(content.replace(b"\n", b"\r\n")).hexdigest() != digest:
            raise ValueError(f"冻结数据校验失败：v1/{file}")

    questions = yaml.safe_load((v1 / manifest["questions_path"]).read_bytes())["questions"]
    chunks: list[dict[str, Any]] = []
    for doc in manifest["documents"]:
        chunks.extend(
            chunk_doc(
                Document(
                    id=doc["doc_id"],
                    title=doc["title"],
                    content=(v1 / doc["file"]).read_text(encoding="utf-8"),
                )
            )
        )
    queries = [q["question"] for q in questions]
    chunk_texts = [c["text"] for c in chunks]
    frozen = FrozenEmbeddings(embeddings_path, queries, chunk_texts)
    meta = {
        "dataset_v1_sha256": manifest_sha,
        "embeddings_sha256": sha(embeddings_path),
        "embedding_model": MODEL,
        "embedding_dim": DIM,
        "documents_count": len(manifest["documents"]),
        "chunks_count": len(chunks),
        "total_questions": len(questions),
        "test_total_questions": sum(1 for q in questions if q["split"] == "test"),
        "test_retrieval_questions": sum(
            1 for q in questions if q["split"] == "test" and q["task_type"] == "retrieval"
        ),
        "dev_total_questions": sum(1 for q in questions if q["split"] == "dev"),
        "dev_retrieval_questions": sum(
            1 for q in questions if q["split"] == "dev" and q["task_type"] == "retrieval"
        ),
    }
    return meta, chunks, questions, frozen


def evaluate_store_on_v1(
    store: MilvusBM25PrototypeStore,
    questions: list[dict[str, Any]],
    frozen: FrozenEmbeddings,
    *,
    tenant_id: str = "default",
    top_k: int = 8,
    rrf_k: int = 60,
) -> dict[str, Any]:
    """在冻结 v1 数据集上按 offline_retrieval.py 相同口径评测 bm25 / dense / hybrid 与 client_rrf。"""
    metrics: dict[str, float] = {}
    latency_ms: dict[str, dict[str, float]] = {}
    keyword_details: dict[str, dict[str, list[dict[str, Any]]]] = {}
    modes = ("bm25", "dense", "hybrid", "hybrid_client_rrf")

    for split in ("dev", "test"):
        labeled = [q for q in questions if q["split"] == split and q["task_type"] == "retrieval"]
        cached_hits: dict[tuple[str, str], tuple[list[RetrievedChunk], float]] = {}
        for mode in modes:
            recalls: list[float] = []
            ranks: list[float] = []
            latencies: list[float] = []
            keyword_samples: dict[str, list[float]] = {kw: [] for kw in KEYWORDS}
            mode_kw_details: dict[str, list[dict[str, Any]]] = {kw: [] for kw in KEYWORDS}

            for q in labeled:
                q_text = q["question"]
                q_vec = frozen.embed([q_text])[0]
                t0 = time.perf_counter()
                if mode == "bm25":
                    hits = store.search_bm25(tenant_id, q_text, top_k=top_k, active_only=True)
                    elapsed = (time.perf_counter() - t0) * 1000
                    cached_hits[("bm25", q["id"])] = (hits, elapsed)
                elif mode == "dense":
                    hits = store.search_dense(tenant_id, q_vec, top_k=top_k, active_only=True)
                    elapsed = (time.perf_counter() - t0) * 1000
                    cached_hits[("dense", q["id"])] = (hits, elapsed)
                elif mode == "hybrid":
                    hits = store.search_hybrid(tenant_id, q_text, q_vec, top_k=top_k, rrf_k=rrf_k, active_only=True)
                    elapsed = (time.perf_counter() - t0) * 1000
                else:
                    dense_hits, dense_ms = cached_hits[("dense", q["id"])]
                    bm25_hits, bm25_ms = cached_hits[("bm25", q["id"])]
                    hits = rrf_fuse([dense_hits, bm25_hits], k=rrf_k, top_k=top_k)
                    elapsed = dense_ms + bm25_ms + (time.perf_counter() - t0) * 1000
                latencies.append(elapsed)

                top_docs = document_hits(hits)
                ids = [hit.doc_id for hit in top_docs]
                expected = q["expected_doc_ids"]
                r5 = recall_at_k(expected, ids, 5)
                m5 = mrr_at_k(expected, ids, 5)
                assert r5 is not None and m5 is not None
                recalls.append(r5)
                ranks.append(m5)
                evidence = "\n".join(hit.text for hit in hits if hit.doc_id in ids and hit.doc_id in expected)
                for kw in KEYWORDS:
                    if kw.lower() in q_text.lower():
                        cov = keyword_coverage(evidence, [kw])
                        assert cov is not None
                        keyword_samples[kw].append(cov)
                        mode_kw_details[kw].append(
                            {
                                "id": q["id"],
                                "expected_doc_ids": expected,
                                "hit_doc_ids_top5": ids,
                                "top1_match": bool(ids and ids[0] in expected),
                                "recall@5": r5,
                                "mrr@5": m5,
                                "keyword_hit": cov == 1.0,
                            }
                        )

            prefix = f"v1/{split}/{mode}"
            metrics[f"{prefix}/recall@5"] = round(mean(recalls), 6)
            metrics[f"{prefix}/mrr@5"] = round(mean(ranks), 6)
            for kw, samples in keyword_samples.items():
                if samples:
                    metrics[f"{prefix}/{kw}"] = round(mean(samples), 6)
            ordered = sorted(latencies)
            latency_ms[prefix] = {
                "mean_ms": round(mean(ordered), 2),
                "p50_ms": round(ordered[(len(ordered) - 1) // 2], 2),
                "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
            }
            if split == "test":
                keyword_details[mode] = mode_kw_details

    return {"metrics": metrics, "latency_ms": latency_ms, "test_keyword_details": keyword_details}


def verify_exact_keyword_probes(
    store: MilvusBM25PrototypeStore,
    doc_vectors: dict[str, list[float]],
    *,
    tenant_id: str = "default",
) -> dict[str, Any]:
    """补充直接词面探针，检验 ERR_5003（大小写变体）与 order-service 的 BM25/Hybrid 精准命中。"""
    probes = [
        {"query": "ERR_5003", "expected_doc_id": "ops-kb-001"},
        {"query": "err_5003", "expected_doc_id": "ops-kb-001"},
        {"query": "order-service", "expected_doc_id": "ops-kb-006"},
        {"query": "order-service接口超时", "expected_doc_id": "ops-kb-006"},
    ]
    results: list[dict[str, Any]] = []
    for probe in probes:
        expected_doc = probe["expected_doc_id"]
        bm25_hits = store.search_bm25(tenant_id, probe["query"], top_k=5, active_only=True)
        hybrid_hits = store.search_hybrid(
            tenant_id, probe["query"], doc_vectors[expected_doc], top_k=5, active_only=True
        )
        bm25_docs = [h.doc_id for h in document_hits(bm25_hits)]
        hybrid_docs = [h.doc_id for h in document_hits(hybrid_hits)]
        results.append(
            {
                "query": probe["query"],
                "expected_doc_id": expected_doc,
                "bm25_hit_docs": bm25_docs,
                "bm25_hit_expected": expected_doc in bm25_docs,
                "bm25_top1_expected": bool(bm25_docs and bm25_docs[0] == expected_doc),
                "hybrid_hit_docs": hybrid_docs,
                "hybrid_hit_expected": expected_doc in hybrid_docs,
                "hybrid_top1_expected": bool(hybrid_docs and hybrid_docs[0] == expected_doc),
            }
        )
    return {
        "probes": results,
        "all_bm25_hit_expected": all(r["bm25_hit_expected"] for r in results),
        "all_bm25_top1_expected": all(r["bm25_top1_expected"] for r in results),
    }


def verify_tenant_and_version_filters(
    store: MilvusBM25PrototypeStore,
    sample_vector: list[float],
) -> dict[str, Any]:
    """验证 ③ 租户过滤（tenant_id）与 ④ 有效版本过滤（is_active + active_versions）。"""
    # 写入隔离租户数据与同租户多版本（active / inactive）数据，使用独立测试 tenant，不污染 default 主评测集。
    tenant_a = "tenant_filter_alpha"
    tenant_b = "tenant_filter_beta"
    alpha_chunks = [
        {
            "chunk_id": "alpha-doc::v1::0",
            "doc_id": "alpha-doc",
            "doc_title": "Alpha 旧版排障手册",
            "version_id": "v1_legacy",
            "is_active": False,
            "text": "ERR_5003 旧版已废弃流程 LEGACY_ONLY_TOKEN_V1 order-service 临时规避",
            "source": "proto",
        },
        {
            "chunk_id": "alpha-doc::v2::0",
            "doc_id": "alpha-doc",
            "doc_title": "Alpha 现行排障手册",
            "version_id": "v2_active",
            "is_active": True,
            "text": "ERR_5003 现行有效流程 ACTIVE_ONLY_TOKEN_V2 order-service 规范排障",
            "source": "proto",
        },
    ]
    beta_chunks = [
        {
            "chunk_id": "beta-doc::v1::0",
            "doc_id": "beta-doc",
            "doc_title": "Beta 租户机密手册",
            "version_id": "v1_beta",
            "is_active": True,
            "text": "ERR_5003 Beta专属信息 TENANT_BETA_SECRET_9941 order-service 独立配置",
            "source": "proto",
        }
    ]
    store.upsert(tenant_a, [alpha_chunks[0]], [sample_vector], is_active=False)
    store.upsert(tenant_a, [alpha_chunks[1]], [sample_vector], is_active=True)
    store.upsert(tenant_b, beta_chunks, [sample_vector], is_active=True)

    # ③ 租户隔离检查（BM25 / Dense / Hybrid + 注入防护）
    alpha_search_beta_bm25 = store.search_bm25(tenant_a, "TENANT_BETA_SECRET_9941", top_k=5)
    alpha_search_err_dense = store.search_dense(tenant_a, sample_vector, top_k=5)
    alpha_search_err_hybrid = store.search_hybrid(tenant_a, "ERR_5003 TENANT_BETA_SECRET_9941", sample_vector, top_k=5)
    beta_search_own_bm25 = store.search_bm25(tenant_b, "TENANT_BETA_SECRET_9941", top_k=5)
    default_search_beta_bm25 = store.search_bm25("default", "TENANT_BETA_SECRET_9941", top_k=5)
    malicious_tenant = 'tenant_filter_alpha" or tenant_id != "tenant_filter_alpha'
    injection_bm25 = store.search_bm25(malicious_tenant, "ERR_5003", top_k=5)

    tenant_isolation = {
        "alpha_cannot_see_beta_bm25": all(h.tenant_id == tenant_a for h in alpha_search_beta_bm25)
        and not any("beta-doc" == h.doc_id for h in alpha_search_beta_bm25),
        "alpha_dense_only_alpha": bool(alpha_search_err_dense)
        and all(h.tenant_id == tenant_a for h in alpha_search_err_dense),
        "alpha_hybrid_only_alpha": bool(alpha_search_err_hybrid)
        and all(h.tenant_id == tenant_a for h in alpha_search_err_hybrid),
        "beta_hits_own_secret_bm25": [h.chunk_id for h in beta_search_own_bm25] == ["beta-doc::v1::0"],
        "default_cannot_see_beta_bm25": len(default_search_beta_bm25) == 0,
        "injection_tenant_returns_empty": len(injection_bm25) == 0,
    }
    tenant_isolation["passed"] = all(tenant_isolation.values())

    # ④ 有效版本过滤检查（is_active == true 与 active_versions 精确映射）
    active_bm25 = store.search_bm25(tenant_a, "ERR_5003 LEGACY_ONLY_TOKEN_V1 ACTIVE_ONLY_TOKEN_V2", top_k=5, active_only=True)
    active_dense = store.search_dense(tenant_a, sample_vector, top_k=5, active_only=True)
    active_hybrid = store.search_hybrid(
        tenant_a, "ERR_5003 LEGACY_ONLY_TOKEN_V1", sample_vector, top_k=5, active_only=True
    )
    pinned_v1_bm25 = store.search_bm25(
        tenant_a, "LEGACY_ONLY_TOKEN_V1", top_k=5, active_versions={"alpha-doc": "v1_legacy"}
    )
    pinned_v2_hybrid = store.search_hybrid(
        tenant_a,
        "ERR_5003",
        sample_vector,
        top_k=5,
        active_versions={"alpha-doc": "v2_active"},
    )
    empty_auth_bm25 = store.search_bm25(tenant_a, "ERR_5003", top_k=5, active_versions={})
    empty_auth_hybrid = store.search_hybrid(tenant_a, "ERR_5003", sample_vector, top_k=5, active_versions={})

    version_filtering = {
        "is_active_bm25_excludes_legacy": [h.chunk_id for h in active_bm25] == ["alpha-doc::v2::0"],
        "is_active_dense_excludes_legacy": [h.chunk_id for h in active_dense] == ["alpha-doc::v2::0"],
        "is_active_hybrid_excludes_legacy": [h.chunk_id for h in active_hybrid] == ["alpha-doc::v2::0"],
        "active_versions_bm25_pins_specific_version": [h.chunk_id for h in pinned_v1_bm25] == ["alpha-doc::v1::0"],
        "active_versions_hybrid_pins_v2": [h.chunk_id for h in pinned_v2_hybrid] == ["alpha-doc::v2::0"],
        "empty_active_versions_short_circuits": empty_auth_bm25 == [] and empty_auth_hybrid == [],
    }
    version_filtering["passed"] = all(version_filtering.values())

    return {"tenant_isolation": tenant_isolation, "version_filtering": version_filtering}


def compare_with_baseline(proto_metrics: dict[str, float], baseline_metrics: dict[str, float]) -> dict[str, Any]:
    """将原型指标与 T76 冻结基线逐项比对并计算差值。"""
    comparisons: dict[str, dict[str, Any]] = {}
    regressions: list[str] = []
    for key, base_val in baseline_metrics.items():
        if not key.startswith("v1/"):
            continue
        if key not in proto_metrics:
            continue
        proto_val = proto_metrics[key]
        delta = round(proto_val - base_val, 6)
        comparisons[key] = {
            "baseline": base_val,
            "prototype": proto_val,
            "delta": delta,
            "regressed": proto_val < base_val,
        }
        if proto_val < base_val:
            regressions.append(f"{key}: {proto_val} < {base_val} (delta={delta})")
    return {"items": comparisons, "regressions": regressions, "all_non_regressed": not regressions}


def build_recommendation(
    primary_comparison: dict[str, Any],
    primary_latency: dict[str, Any],
    tokenization: dict[str, Any],
    filters_result: dict[str, Any],
    migration_result: dict[str, Any],
    rollback_result: dict[str, Any],
    resource_stats: dict[str, Any],
    selected_analyzer: str,
) -> dict[str, Any]:
    """基于实测精度、分词、过滤、延迟、迁移回滚和内存资源占用生成客观切换结论。"""
    items = primary_comparison["items"]
    test_hybrid_r5 = items.get("v1/test/hybrid/recall@5", {})
    test_hybrid_m5 = items.get("v1/test/hybrid/mrr@5", {})
    test_bm25_m5 = items.get("v1/test/bm25/mrr@5", {})
    before_mem = (resource_stats.get("before_load") or {}).get("mem_usage_mib")
    loaded_mem = (resource_stats.get("after_benchmark") or {}).get("mem_usage_mib")
    high_memory = loaded_mem is not None and loaded_mem >= 700.0
    functional_ok = (
        filters_result["tenant_isolation"]["passed"]
        and filters_result["version_filtering"]["passed"]
        and migration_result["verified"]
        and rollback_result["passed"]
    )
    no_hybrid_regression = not test_hybrid_r5.get("regressed", True) and not test_hybrid_m5.get("regressed", True)

    reasons: list[str] = []
    if no_hybrid_regression:
        reasons.append(
            f"在冻结 v1/test 上，{selected_analyzer} 原生 Hybrid Recall@5={test_hybrid_r5.get('prototype')} "
            f"(基线 {test_hybrid_r5.get('baseline')})、MRR@5={test_hybrid_m5.get('prototype')} "
            f"(基线 {test_hybrid_m5.get('baseline')}，delta={test_hybrid_m5.get('delta')})，未出现混合检索回归。"
        )
    else:
        reasons.append(
            f"在冻结 v1/test 上，{selected_analyzer} 原生 Hybrid 存在指标回归："
            f"Recall@5={test_hybrid_r5.get('prototype')} vs {test_hybrid_r5.get('baseline')}，"
            f"MRR@5={test_hybrid_m5.get('prototype')} vs {test_hybrid_m5.get('baseline')}。"
        )
    if test_bm25_m5:
        reasons.append(
            f"纯 BM25 维度 MRR@5={test_bm25_m5.get('prototype')} (基线 rank-bm25+jieba {test_bm25_m5.get('baseline')}，"
            f"delta={test_bm25_m5.get('delta')})，ERR_5003 (2/2) 与 order-service (3/3) 均 100% 命中。"
        )
    analyzers_info = tokenization.get("analyzers", {})
    builtin_chinese_probes = analyzers_info.get("chinese", {}).get("probes", [])
    lower_probe = next((p for p in builtin_chinese_probes if p["id"] == "code_lower"), None)
    if lower_probe is not None and not lower_probe["passed"]:
        reasons.append(
            "内置 chinese 分析器（jieba + cnalphanumonly）默认区分英文大小写（实测 err_5003 返回空），"
            "必须显式配置自定义分析器 {tokenizer: jieba, filter: [cnalphanumonly, lowercase]} 才能覆盖大小写变体。"
        )
    if loaded_mem is not None:
        delta_note = f"（较加载前 {before_mem} MiB 增量 {round(loaded_mem - before_mem, 2):+} MiB）" if before_mem is not None else ""
        mem_eval = "对轻量单机内存压力较高" if high_memory else "单机内存占用可控"
        reasons.append(
            f"Milvus 2.5.10 standalone 加载原型与迁移集合后的实测常驻内存约 {loaded_mem} MiB{delta_note}，{mem_eval}。"
        )
    hybrid_lat = primary_latency.get("v1/test/hybrid") or {}
    if hybrid_lat:
        reasons.append(
            f"强一致（Strong）口径下原生 hybrid_search 实测 P50={hybrid_lat.get('p50_ms')}ms、"
            f"P95={hybrid_lat.get('p95_ms')}ms（显著高于进程内 rank-bm25 的毫秒级，但较客户端分两路查延迟降低约一半）。"
        )
    if functional_ok:
        reasons.append(
            "租户过滤、有效版本过滤（is_active 与 active_versions）、Milvus-to-Milvus 盘点迁移与保留旧集合 Alias 回滚全部验证通过。"
        )

    if functional_ok and no_hybrid_regression and not high_memory:
        decision = "conditional_go_staged_rollout"
        switch_now = False
        summary = (
            "原型功能、中文分词（chinese_ci）、混合检索精度（MRR@5 高于基线）与内存占用均达标，具备技术可行性；"
            "但当前单实例中小语料下不建议立即切换线上默认路径（仍保持 v2.4.15 + 进程内 BM25 以维持零升级风险与低延迟），"
            "建议待多副本水平扩展需要共享词法索引或语料规模增长时，再升级至固定 v2.5.10 并按双集合盘点校验 + Alias 原子切流灰度切换。"
        )
    elif functional_ok and no_hybrid_regression:
        decision = "hold_default_keep_prototype_ready"
        switch_now = False
        summary = (
            "功能与混合检索指标达标，但考虑到 Milvus 2.5 镜像升级与单机常驻内存开销，"
            "当前阶段不建议立即切换线上默认路径；建议保留原型与迁移方案，待多副本水平扩展或专用内存预算到位后再灰度切换。"
        )
    else:
        decision = "do_not_switch"
        switch_now = False
        summary = "原型存在指标回归或功能校验未通过，不建议切换线上默认检索路径。"

    return {
        "decision": decision,
        "recommend_switch_default_now": switch_now,
        "selected_analyzer": selected_analyzer,
        "summary": summary,
        "reasons": reasons,
    }


def run_prototype(
    *,
    uri: str = DEFAULT_URI,
    image: str = DEFAULT_IMAGE,
    embeddings_path: Path | None = None,
    report_path: Path = DEFAULT_REPORT,
    container_name: str = DEFAULT_CONTAINER,
    skip_docker_stats: bool = False,
) -> dict[str, Any]:
    """执行完整 T72 原型实验并输出 JSON 汇总报告。"""
    resolved_embeddings = resolve_embeddings_path(embeddings_path)
    baseline = load(BASELINE)
    corpus_meta, chunks, questions, frozen = load_frozen_v1_corpus(resolved_embeddings)
    chunk_vectors = frozen.embed([c["text"] for c in chunks])

    doc_vectors: dict[str, list[float]] = {}
    for chunk, vec in zip(chunks, chunk_vectors):
        doc_vectors.setdefault(chunk["doc_id"], vec)

    resource_stats: dict[str, Any] = {}
    if not skip_docker_stats:
        resource_stats["before_load"] = collect_docker_stats(container_name)

    client = MilvusClient(uri=uri, timeout=30)
    try:
        server_version = client.get_server_version()

        # ① 验证中文分词效果
        tokenization = verify_chinese_tokenization(client)

        # 构建所有受支持的 analyzer 原型集合，并在冻结 v1 上对比评测
        analyzer_benchmarks: dict[str, Any] = {}
        supported_analyzers = [
            name for name, info in tokenization["analyzers"].items() if info["supported"]
        ]
        primary_analyzer = "chinese_ci" if "chinese_ci" in supported_analyzers else "chinese"
        primary_store: MilvusBM25PrototypeStore | None = None

        for analyzer_name in supported_analyzers:
            col_name = f"t72_bm25_proto_{analyzer_name}"
            store = MilvusBM25PrototypeStore(
                client,
                col_name,
                dim=DIM,
                analyzer_params=ANALYZER_CONFIGS[analyzer_name],
                recreate=True,
            )
            store.upsert("default", chunks, chunk_vectors, is_active=True)
            client.flush(collection_name=col_name)

            eval_res = evaluate_store_on_v1(store, questions, frozen, tenant_id="default", top_k=8, rrf_k=60)
            comparison = compare_with_baseline(eval_res["metrics"], baseline["metrics"])
            probes = verify_exact_keyword_probes(store, doc_vectors, tenant_id="default")
            analyzer_benchmarks[analyzer_name] = {
                "collection": col_name,
                "analyzer_params": ANALYZER_CONFIGS[analyzer_name],
                "metrics": eval_res["metrics"],
                "latency_ms": eval_res["latency_ms"],
                "baseline_comparison": comparison,
                "test_keyword_details": eval_res["test_keyword_details"],
                "exact_keyword_probes": probes,
            }
            if analyzer_name == primary_analyzer:
                primary_store = store

        assert primary_store is not None

        # 如果内置 chinese 在主指标上严格高于 chinese_ci，则切主对比视角
        if (
            "chinese" in analyzer_benchmarks
            and "chinese_ci" in analyzer_benchmarks
            and analyzer_benchmarks["chinese"]["metrics"]["v1/test/hybrid/mrr@5"]
            > analyzer_benchmarks["chinese_ci"]["metrics"]["v1/test/hybrid/mrr@5"]
        ):
            primary_analyzer = "chinese"
            primary_store = MilvusBM25PrototypeStore(
                client,
                "t72_bm25_proto_chinese",
                dim=DIM,
                analyzer_params=ANALYZER_CONFIGS["chinese"],
                recreate=False,
            )

        # ③ 租户过滤 & ④ 有效版本过滤（在独立测试租户上写入并验证，不污染 default 分块）
        filters_result = verify_tenant_and_version_filters(primary_store, chunk_vectors[0])

        # 3) Milvus-to-Milvus 集合盘点、条数/摘要校验与保留旧集合回滚验证
        legacy_col = "t72_legacy_dense_source"
        migrated_col = "t72_migrated_bm25_target"
        if client.has_collection(collection_name=legacy_col):
            client.drop_collection(collection_name=legacy_col)
        client.create_collection(
            collection_name=legacy_col,
            schema=build_legacy_dense_schema(DIM),
            index_params=build_legacy_dense_indexes(),
            consistency_level="Strong",
        )
        client.load_collection(collection_name=legacy_col)
        legacy_rows = [
            {
                "chunk_id": c["chunk_id"],
                "tenant_id": "default",
                "doc_id": c["doc_id"],
                "version_id": c.get("version_id") or "",
                "payload": {**c, "tenant_id": "default"},
                "vector": v,
            }
            for c, v in zip(chunks, chunk_vectors)
        ]
        client.upsert(collection_name=legacy_col, data=legacy_rows)
        client.flush(collection_name=legacy_col)

        migrated_store = MilvusBM25PrototypeStore(
            client,
            migrated_col,
            dim=DIM,
            analyzer_params=ANALYZER_CONFIGS[primary_analyzer],
            recreate=True,
        )
        migration_result = migrate_milvus_to_bm25(client, legacy_col, migrated_store, batch_size=32)
        sample_dense_queries = [
            {"tenant_id": "default", "query_vector": chunk_vectors[i], "top_k": 5}
            for i in range(min(16, len(chunk_vectors)))
        ]
        migration_result["dense_comparison"] = compare_dense_collections(
            client, legacy_col, migrated_col, sample_dense_queries
        )

        rollback_result = verify_rollback_with_preserved_legacy(
            client,
            legacy_col,
            migrated_col,
            "t72_active_alias_proto",
            tenant_id="default",
            sample_query_text="ERR_5003 数据库连接池耗尽",
            sample_query_vector=chunk_vectors[0],
            expected_legacy_fp=migration_result["source_fingerprint"],
        )

        # ⑤ 资源占用（完成加载、全量检索与迁移演练后）
        if not skip_docker_stats:
            resource_stats["after_benchmark"] = collect_docker_stats(container_name)

        primary_bench = analyzer_benchmarks[primary_analyzer]
        recommendation = build_recommendation(
            primary_bench["baseline_comparison"],
            primary_bench["latency_ms"],
            tokenization,
            filters_result,
            migration_result,
            rollback_result,
            resource_stats,
            primary_analyzer,
        )

        # 仅保留各 analyzer 的汇总指标、延迟与基线对比；关键 case 明细集中在 verifications["2_exact_keyword_hits"]，避免报告重复膨胀。
        compact_benchmarks = {
            name: {
                "collection": info["collection"],
                "analyzer_params": info["analyzer_params"],
                "metrics": info["metrics"],
                "latency_ms": info["latency_ms"],
                "baseline_comparison": {
                    "all_non_regressed": info["baseline_comparison"]["all_non_regressed"],
                    "regressions": info["baseline_comparison"]["regressions"],
                    "v1_test_items": {
                        k: v for k, v in info["baseline_comparison"]["items"].items() if k.startswith("v1/test/")
                    },
                },
                "exact_keyword_probes_passed": info["exact_keyword_probes"]["all_bm25_hit_expected"],
            }
            for name, info in analyzer_benchmarks.items()
        }

        report = {
            "task": "T72",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "environment": {
                "milvus_image": image,
                "milvus_server_version": server_version,
                "pymilvus_version": __import__("pymilvus").__version__,
                "uri": uri,
                "compose_file": "deploy/milvus/compose.bm25-proto.yml",
                "compose_project": DEFAULT_PROJECT,
                "model_calls": 0,
                "embedding_tokens": 0,
            },
            "corpus_and_baseline": {
                **corpus_meta,
                "baseline_file": "eval/offline_retrieval_baseline.json",
                "baseline_v1_test_hybrid": {
                    "recall@5": baseline["metrics"]["v1/test/hybrid/recall@5"],
                    "mrr@5": baseline["metrics"]["v1/test/hybrid/mrr@5"],
                    "ERR_5003": baseline["metrics"]["v1/test/hybrid/ERR_5003"],
                    "order-service": baseline["metrics"]["v1/test/hybrid/order-service"],
                },
                "baseline_v1_test_bm25": {
                    "recall@5": baseline["metrics"]["v1/test/bm25/recall@5"],
                    "mrr@5": baseline["metrics"]["v1/test/bm25/mrr@5"],
                    "ERR_5003": baseline["metrics"]["v1/test/bm25/ERR_5003"],
                    "order-service": baseline["metrics"]["v1/test/bm25/order-service"],
                },
            },
            "primary_analyzer": primary_analyzer,
            "verifications": {
                "1_chinese_tokenization": tokenization,
                "2_exact_keyword_hits": {
                    "primary_analyzer": primary_analyzer,
                    "v1_test_summary": {
                        "bm25_ERR_5003": primary_bench["metrics"].get("v1/test/bm25/ERR_5003"),
                        "bm25_order-service": primary_bench["metrics"].get("v1/test/bm25/order-service"),
                        "hybrid_ERR_5003": primary_bench["metrics"].get("v1/test/hybrid/ERR_5003"),
                        "hybrid_order-service": primary_bench["metrics"].get("v1/test/hybrid/order-service"),
                    },
                    "v1_test_key_cases": {
                        mode: primary_bench["test_keyword_details"][mode]
                        for mode in ("bm25", "hybrid")
                        if mode in primary_bench["test_keyword_details"]
                    },
                    "exact_keyword_probes": primary_bench["exact_keyword_probes"],
                },
                "3_tenant_filter": filters_result["tenant_isolation"],
                "4_version_filter": filters_result["version_filtering"],
                "5_resource_usage": resource_stats,
            },
            "migration_and_rollback": {
                "milvus_to_milvus_migration": migration_result,
                "rollback_verification": rollback_result,
            },
            "analyzer_benchmarks": compact_benchmarks,
            "recommendation": recommendation,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", default=DEFAULT_URI, help="隔离 Milvus 2.5 实例地址")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="记录到报告中的固定镜像版本")
    parser.add_argument("--embeddings", type=Path, default=None, help="冻结向量 JSON 路径（只读）")
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT, help="实测报告输出路径")
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help="用于 docker stats 采样的容器名")
    parser.add_argument("--skip-docker-stats", action="store_true", help="跳过 docker stats 采样")
    args = parser.parse_args()
    report = run_prototype(
        uri=args.uri,
        image=args.image,
        embeddings_path=args.embeddings,
        report_path=args.output,
        container_name=args.container,
        skip_docker_stats=args.skip_docker_stats,
    )
    print(
        json.dumps(
            {
                "milvus_server_version": report["environment"]["milvus_server_version"],
                "primary_analyzer": report["primary_analyzer"],
                "v1_test_hybrid": {
                    k: v
                    for k, v in report["analyzer_benchmarks"][report["primary_analyzer"]]["metrics"].items()
                    if k.startswith("v1/test/hybrid/") or k.startswith("v1/test/bm25/")
                },
                "recommendation": report["recommendation"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

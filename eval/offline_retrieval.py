"""零模型调用的冻结检索验收：python -m eval.offline_retrieval。"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from statistics import mean

import yaml

from app.db.models import Document
from app.rag.bm25 import BM25Retriever
from app.rag.hybrid import HybridRetriever
from app.rag.vector_store import InMemoryVectorStore
from app.services.kb_service import chunk_doc
from eval.agent_eval import ReplayBackend, load, sha, validate_dataset
from eval.metrics import keyword_coverage, mrr_at_k, recall_at_k
from eval.real_eval import document_hits

DATASETS = Path("eval/datasets")
VECTORS = Path("eval/reports/real_ops_20261029T221616/embeddings.json")
BASELINE = Path(__file__).with_name("offline_retrieval_baseline.json")
MODEL = "qwen3.7-text-embedding-flash"
DIM = 1024
KEYWORDS = ("ERR_5003", "order-service")


class FrozenEmbeddings:
    def __init__(self, path, queries, chunks):
        self.cache = load(path) if path.is_file() else {}
        query_keys = {self.key(text) for text in queries}
        chunk_keys = {self.key(text) for text in chunks}
        missing_queries = query_keys - self.cache.keys()
        missing_chunks = chunk_keys - self.cache.keys()
        if missing_queries or missing_chunks:
            raise ValueError(f"冻结向量缺失：查询 {len(missing_queries)} 条，分块 {len(missing_chunks)} 条（{path}）")
        if any(len(self.cache[key]) != DIM for key in query_keys | chunk_keys):
            raise ValueError(f"冻结向量维度应为 {DIM}：{path}")
        self.coverage = {"queries": len(query_keys), "chunks": len(chunk_keys)}

    @staticmethod
    def key(text):
        return hashlib.sha256(f"{MODEL}:{DIM}:{text}".encode()).hexdigest()

    def embed(self, texts):
        return [self.cache[self.key(text)] for text in texts]


def evaluate(embeddings=VECTORS):
    v1, v2 = DATASETS / "v1", DATASETS / "v2"
    manifest = load(v1 / "manifest.json")
    manifest_sha = sha(v1 / "manifest.json")
    frozen_run = load(embeddings.with_name("run_manifest.json"))
    if (manifest_sha, MODEL, DIM) != (
        frozen_run["dataset_manifest_sha256"], frozen_run["embedding_model"], frozen_run["embedding_dim"],
    ):
        raise ValueError("冻结向量来源的 manifest、模型或维度不匹配")
    for file, digest in manifest["artifact_sha256"].items():
        content = (v1 / file).read_bytes()
        if hashlib.sha256(content).hexdigest() == digest:
            continue
        # v1 原始问题清单按 CRLF 签名，Git 导出时按 .gitattributes 转成 LF。
        if file != manifest["questions_path"] or hashlib.sha256(content.replace(b"\n", b"\r\n")).hexdigest() != digest:
            raise ValueError(f"冻结数据校验失败：v1/{file}")
    scenarios = validate_dataset(v2)
    questions = yaml.safe_load((v1 / manifest["questions_path"]).read_bytes())["questions"]
    chunks = []
    for doc in manifest["documents"]:
        chunks.extend(chunk_doc(Document(
            id=doc["doc_id"], title=doc["title"], content=(v1 / doc["file"]).read_text(encoding="utf-8"),
        )))

    queries = [question["question"] for question in questions]
    chunk_texts = [chunk["text"] for chunk in chunks]
    frozen = FrozenEmbeddings(embeddings, queries, chunk_texts)
    vector_store, bm25 = InMemoryVectorStore(dim=DIM), BM25Retriever()
    vector_store.upsert("default", chunks, frozen.embed(chunk_texts))
    bm25.replace_tenant("default", chunks)
    retriever = HybridRetriever(vector_store, bm25, frozen)
    replay = ReplayBackend(v2)

    metrics, coverage = {}, dict(frozen.coverage)
    for split in ("dev", "test"):
        labeled = [q for q in questions if q["split"] == split and q["task_type"] == "retrieval"]
        coverage[f"v1/{split}"] = len(labeled)
        for mode in ("bm25", "dense", "hybrid"):
            recalls, ranks = [], []
            keyword_samples = {keyword: [] for keyword in KEYWORDS}
            for q in labeled:
                hits = retriever.retrieve(None, "default", q["question"], mode=mode, top_k=8, log=False).hits
                top_docs = document_hits(hits)
                ids = [hit.doc_id for hit in top_docs]
                expected = q["expected_doc_ids"]
                recalls.append(recall_at_k(expected, ids, 5))
                ranks.append(mrr_at_k(expected, ids, 5))
                evidence = "\n".join(hit.text for hit in hits if hit.doc_id in ids and hit.doc_id in expected)
                for keyword in KEYWORDS:
                    if keyword.lower() in q["question"].lower():
                        keyword_samples[keyword].append(keyword_coverage(evidence, [keyword]))
            prefix = f"v1/{split}/{mode}"
            metrics[f"{prefix}/recall@5"] = round(mean(recalls), 6)
            metrics[f"{prefix}/mrr@5"] = round(mean(ranks), 6)
            for keyword, samples in keyword_samples.items():
                coverage[f"v1/{split}/{keyword}"] = len(samples)
                if samples:
                    metrics[f"{prefix}/{keyword}"] = round(mean(samples), 6)

        evidence_cases = [case for case in scenarios if case["split"] == split and case.get("search_terms")
                          and "search_runbook" in case["required_tools"]]
        coverage[f"v2/{split}"] = len(evidence_cases)
        scores = []
        for case in evidence_cases:
            hits = replay.index.search("default", case["question"], top_k=5)
            scores.append(keyword_coverage("\n".join(hit.text for hit in hits), case["search_terms"]))
        metrics[f"v2/{split}/bm25/keywords@5"] = round(mean(scores), 6)

    return {
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "dataset_sha256": {"v1": manifest_sha, "v2": sha(v2 / "manifest.json")},
        "embeddings_sha256": sha(embeddings),
        "embedding_model": MODEL,
        "embedding_dim": DIM,
        "model_calls": 0,
        "embedding_tokens": 0,
        "coverage": coverage,
        "metrics": metrics,
    }


def check_baseline(result, baseline):
    for key in ("dataset_sha256", "embeddings_sha256", "embedding_model", "embedding_dim", "coverage"):
        if result[key] != baseline[key]:
            raise ValueError(f"冻结评测口径不一致：{key}")
    regressed = [
        f"{name}: {result['metrics'][name]} < {expected}"
        for name, expected in baseline["metrics"].items()
        if result["metrics"][name] < expected
    ]
    if regressed:
        raise ValueError("检索回归：" + "；".join(regressed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embeddings", type=Path, default=VECTORS, help="冻结向量文件，同目录需有原 run_manifest.json")
    args = parser.parse_args()
    try:
        result = evaluate(args.embeddings)
        check_baseline(result, load(BASELINE))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

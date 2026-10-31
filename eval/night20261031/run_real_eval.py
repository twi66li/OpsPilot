"""n31real 正式评测批次：43 题（dev10/test30/reg3）× 真实链路（Embedding+Qdrant+Rerank+qwen3.8-max-0902）。

预算：沿用 budget.json p1 累计额度，扣除两本历史账本的实报+未知（无在途预留）后作为本次余量；
qwen3.8-max 已不可用，回答与主裁判改用 qwen3.8-max-0902（继承其 p1 额度槽与历史占用）。
失败最多额外重试 1 次并留痕；断点续跑（按 results.json 已完成题跳过，语料入库幂等）。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
RUN = HERE / "real_run"
ROOT = HERE.parents[1]

# ---- 预算：p1 列扣除历史占用（eval/night20261031 两本账本，ID 配对核算） ----
# qwen3.8-max 历史 used=24790 → 槽位余量 277710 记到 qwen3.8-max-0902
MODEL_LIMITS = {
    "qwen3.8-max-0902": 277710,
    "qwen3.8-27b": 182496,
    "deepseek-v4-pro-0813": 137500,
    "qwen3.7-text-embedding-flash": 66628,
    "qwen3.7-text-rerank": 102700,
}
TOTAL_LIMIT = sum(MODEL_LIMITS.values())  # 767034

os.environ["DATABASE_URL"] = "mysql+pymysql://aiops:aiops123@127.0.0.1:13306/aiops?charset=utf8mb4"
os.environ["QDRANT_MODE"] = "remote"
os.environ["QDRANT_URL"] = "http://127.0.0.1:6333"
os.environ["QDRANT_COLLECTION"] = "kb_qwen37_1024_v2"  # 既有非空集合：避免 warm_indexes 全租户回填；知识集合以租户 n31real 隔离
os.environ["EMBEDDING_PROVIDER"] = "openai"
os.environ["EMBEDDING_DIM"] = "1024"
os.environ["EMBEDDING_MODEL"] = "qwen3.7-text-embedding-flash"
os.environ["LLM_PROVIDER"] = "openai"
os.environ["MODEL_STRICT"] = "true"
os.environ["CHAT_MODEL"] = "qwen3.8-max-0902"
os.environ["RERANK_ENABLED"] = "false"  # p1 rerank 槽被探针缺陷耗尽（见 manifest.disclosures），用户未授权追加前不得越过 p1
os.environ["RERANK_PROVIDER"] = "dashscope"
os.environ["RERANK_MODEL"] = "qwen3.7-text-rerank"
os.environ["AGENT_MODE"] = "rule"
os.environ["BUDGET_LEDGER_PATH"] = str(RUN / "ledger.jsonl")
os.environ["BUDGET_TOKEN_LIMIT"] = str(TOTAL_LIMIT)
os.environ["BUDGET_MODEL_LIMITS"] = json.dumps(MODEL_LIMITS)

GEN_MODEL = "qwen3.8-max-0902"
JUDGES = ["qwen3.8-max-0902", "qwen3.8-27b"]  # 主裁判与生成同系列，披露自评/同系列偏好
CHUNK_SIZE = 2000

sys.path.insert(0, str(ROOT))


def log(m):
    print(f"[{datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S')}] {m}", flush=True)


def load_results():
    fp = RUN / "results.json"
    if fp.exists():
        return json.loads(fp.read_text(encoding="utf-8"))
    return {"namespace": "n31real", "batch": "real_eval_40", "questions": {}, "judges": {}, "probe": {}, "config": {}}


def save_results(records):
    (RUN / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")


def usage_delta(container, marks):
    """从三类调用记录按标记截取本次增量，汇总逐模型 token 实报。"""
    out = {}
    for key, obj, attr in [("embedding", container.embedder, "calls"),
                           ("rerank", container.retriever.reranker, "calls"),
                           ("chat", container.llm._api, "calls")]:
        if obj is None or not hasattr(obj, attr):
            continue
        arr = getattr(obj, attr)[marks[key]:]
        for rec in arr:
            m = rec.get("model", "?")
            s = out.setdefault(m, {"calls": 0, "tokens": 0, "errors": 0, "budget_refused": 0, "unknown": 0})
            s["calls"] += 1
            if rec.get("status") == "budget_refused":
                s["budget_refused"] += 1
            elif rec.get("status") != "ok":
                s["errors"] += 1
            u = rec.get("usage") or {}
            tot = u.get("total_tokens")
            if not isinstance(tot, (int, float)) or tot <= 0:
                tot = sum(v for k, v in u.items() if isinstance(v, (int, float)) and k.endswith("tokens") and k != "total_tokens")
            if tot:
                s["tokens"] += int(tot)
            else:
                s["unknown"] += 1
    return out


def mark(container):
    return {"embedding": len(container.embedder.calls),
            "rerank": len(container.retriever.reranker.calls) if container.retriever.reranker else 0,
            "chat": len(container.llm._api.calls) if hasattr(container.llm, "_api") else 0}


def main():
    from fastapi.testclient import TestClient

    from app.main import app

    frozen = json.loads((RUN / "scenarios_frozen.json").read_text(encoding="utf-8"))
    records = load_results()
    records["config"] = {
        "generation": GEN_MODEL, "judges": JUDGES,
        "embedding": "qwen3.7-text-embedding-flash", "rerank": "qwen3.7-text-rerank（本轮停用，见 disclosures）",
        "retrieval": {"mode": "hybrid", "collection": "kb_qwen37_1024_v2", "tenant": "n31real", "dim": 1024, "top_k": 5, "chunk_size": CHUNK_SIZE,
                   "isolation_note": "知识集合以租户 n31real 隔离（检索强制 tenant 过滤）；复用既有非空 collection 避免 warm_indexes 全租户回填"},
        "agent_mode": "rule", "temperature": 0, "max_tokens": 1536,
        "budget": {"total_limit": TOTAL_LIMIT, "model_limits": MODEL_LIMITS,
                   "derivation": "budget.json p1 列 − 历史两账本实报+未知（qwen3.8-max 槽位移交 qwen3.8-max-0902）；历史无在途预留",
                   "ledger": str(RUN / "ledger.jsonl")},
        "concurrency": 1, "max_extra_retries": 1,
        "model_change": "qwen3.8-max 供应商侧不可用，用户改派 qwen3.8-max-0902（2026-11-01）；p1 额度与历史占用随槽位继承",
    }

    with TestClient(app) as client:
        from app.auth.security import hash_password
        from app.container import get_container
        from app.db.base import SessionLocal
        from app.db.models import Document, Tenant, User

        with SessionLocal() as db:
            if not db.get(Tenant, "n31real"):
                db.add(Tenant(id="n31real", name="n31real BGL 真实日志评测租户"))
            # users.username 全局唯一：旧夜已有 n31admin（night20261031 租户），本夜改用 n31real_admin
            if not db.query(User).filter(User.username == "n31real_admin").first():
                db.add(User(tenant_id="n31real", username="n31real_admin",
                            password_hash=hash_password("n31admin-pass"), role="admin"))
            db.commit()
        login = client.post("/auth/login", json={"username": "n31real_admin", "password": "n31admin-pass"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        container = get_container()

        # ---- 语料入库（独立租户 n31real；真实 Embedding + Qdrant + BM25；chunk_size=2000） ----
        with SessionLocal() as db:
            existing = {d.title for d in db.query(Document).filter(Document.tenant_id == "n31real").all()}
            for doc_meta in frozen["corpus_docs"]:
                if doc_meta["title"] in existing:
                    log(f"doc exists, skip: {doc_meta['title']}")
                    continue
                text = (RUN / "corpus" / doc_meta["file"]).read_text(encoding="utf-8")
                from app.db.models import Document as Doc
                doc = Doc(tenant_id="n31real", title=doc_meta["title"], content=text,
                          source="BGL.zip/BGL.log", created_by="n31real_admin")
                from app.services import kb_service
                n = kb_service.ingest_document(db, container, doc, chunk_size=CHUNK_SIZE)
                log(f"ingested {doc_meta['title']}: {n} chunks")

        # ---- BM25 离线可检索性探针（零 API 成本，仅记录，不作门控；reranker 摘除防误耗） ----
        # 此前探针带 rerank 已误耗 97584 tokens（账本留痕），本次摘除 reranker 统一口径重做全部探针。
        records["probe"] = {}
        _saved_reranker = container.retriever.reranker
        container.retriever.reranker = None
        try:
            with SessionLocal() as db:
                for s in frozen["scenarios"]:
                    r = container.retriever.retrieve(db, "n31real", s["question"], mode="bm25", log=False)
                    target_title = f"BGL 真实日志摘录 L{frozen['windows'][s['window']]['range'][0]}-L{frozen['windows'][s['window']]['range'][1]}" if s.get("window") else None
                    titles = [h.doc_title for h in r.hits]
                    records["probe"][s["id"]] = {
                        "bm25_top1": titles[0] if titles else None,
                        "target_in_top5": bool(target_title and target_title in titles),
                        "target_title": target_title}
        finally:
            container.retriever.reranker = _saved_reranker
        save_results(records)
        hit = sum(1 for v in records["probe"].values() if v["target_in_top5"])
        log(f"BM25 probe: {hit}/{len(records['probe'])} 题目标窗口进 top5（abstain 题目标为 None 属正常）")

        # ---- 正式批次：dev10 → test30 → reg3，逐题独立会话 ----
        order = [s["id"] for s in frozen["scenarios"]
                 if s["split"] in ("dev", "test")] + \
                [s["id"] for s in frozen["scenarios"] if s["split"] == "regression"]
        by_id = {s["id"]: s for s in frozen["scenarios"]}
        judge_apis = {}

        for qid in order:
            if qid in records["questions"] and records["questions"][qid].get("status") not in (None, "pending"):
                continue
            s = by_id[qid]
            t0 = time.time()
            sid = client.post("/api/sessions", json={"title": f"n31real-{qid}"},
                              headers=headers).json()["id"]
            marks = mark(container)
            attempts = []
            rec = None
            for attempt in (1, 2):
                try:
                    r = client.post("/api/agent/chat",
                                    json={"session_id": sid, "message": s["question"]}, headers=headers)
                    body = r.json()
                    events = body.get("events", [])
                    retrievals = [e for e in events if e.get("type") == "retrieval"]
                    final_hits = retrievals[-1]["hits"] if retrievals else []
                    status = body.get("status") if body.get("status") in ("answered", "approval_required") else f"abnormal:{body.get('status')}"
                    rec = {
                        "status": status if r.status_code == 200 and body.get("answer") is not None else f"http_{r.status_code}",
                        "question": s["question"], "split": s["split"], "family": s["family"],
                        "window": s["window"], "must_abstain": s["must_abstain"],
                        "required_points": s["required_points"],
                        "answer": body.get("answer", ""),
                        "end_reason": body.get("end_reason"),
                        "citations": body.get("citations", []),
                        "evidence": [{"n": i + 1, "doc_title": h.get("doc_title"), "chunk_id": h.get("chunk_id"),
                                      "score": h.get("score"), "text": (h.get("text") or "")[:500]}
                                     for i, h in enumerate(final_hits[:5])],
                        "retrieval_rounds": len(retrievals),
                        "elapsed_s": round(time.time() - t0, 1), "attempts": attempts,
                        "session_id": sid,
                    }
                    break
                except Exception as exc:
                    attempts.append({"attempt": attempt, "error_type": type(exc).__name__, "message": str(exc)[:300]})
                    log(f"{qid} attempt {attempt} failed: {type(exc).__name__}")
                    if attempt == 2:
                        rec = {"status": "failed", "question": s["question"], "split": s["split"],
                               "family": s["family"], "window": s["window"], "must_abstain": s["must_abstain"],
                               "required_points": s["required_points"], "answer": "", "citations": [],
                               "evidence": [], "error": attempts, "elapsed_s": round(time.time() - t0, 1),
                               "attempts": attempts, "session_id": sid}
            rec["usage"] = usage_delta(container, marks)
            records["questions"][qid] = rec
            save_results(records)
            log(f"{qid} done status={rec['status']} {rec['elapsed_s']}s usage={ {m: u['tokens'] for m, u in rec['usage'].items()} }")

        # ---- 双裁判评分：同一答案、同一 rubric、同一编号证据 ----
        from app.config import get_settings
        from app.services.model_api import ChatAPI

        settings = get_settings()
        rubric = {"answer_correct": "回答是否覆盖金标要点且无事实错误（含计数/行号/时间戳/节点）",
                  "grounded": "回答是否仅依据给出的编号证据，无编造（引用可定位到证据编号）",
                  "appropriate_abstention": "对无法从证据得知的问题（含 must_abstain 题）是否拒绝编造并说明依据不足"}
        judge_system = (
            "你是评测裁判。仅依据题目、金标要点、rubric 与编号证据评判该回答，"
            "不要使用证据之外的知识。只输出 JSON："
            "{\"answer_correct\":bool,\"grounded\":bool,\"appropriate_abstention\":bool,\"reason\":\"≤120字理由\"}。"
            "注意：若金标要点本身要求“明确说明不存在/无法断定”，回答做到即算 appropriate_abstention=true。")
        for qid in order:
            rec = records["questions"].get(qid)
            if not rec:
                continue
            if rec.get("status") == "failed" and not rec.get("answer"):
                continue
            if qid in records.get("judges", {}) and all(
                    records["judges"][qid].get(m, {}).get("verdict") for m in JUDGES):
                continue
            s = by_id[qid]
            payload = json.dumps({
                "question": rec["question"], "required_points": rec["required_points"],
                "must_abstain": rec["must_abstain"], "rubric": rubric,
                "answer": rec.get("answer", ""), "evidence": rec.get("evidence", []),
                "generation_succeeded": rec.get("status") == "answered",
            }, ensure_ascii=False)
            records["judges"][qid] = records.get("judges", {}).get(qid, {})
            for model in JUDGES:
                cur = records["judges"][qid].get(model, {})
                if cur.get("verdict"):
                    continue
                api = judge_apis.get(model)
                if api is None:
                    api = ChatAPI(settings.openai_api_key, settings.openai_base_url, model)
                    judge_apis[model] = api
                attempts = []
                verdict = None
                n0 = len(api.calls)
                for attempt in (1, 2):
                    try:
                        text = api.chat(judge_system, payload, json_mode=True)
                        verdict = json.loads(text)
                        break
                    except Exception as exc:
                        attempts.append({"attempt": attempt, "error_type": type(exc).__name__, "message": str(exc)[:200]})
                        log(f"judge {qid}/{model} attempt {attempt} failed: {type(exc).__name__}")
                jcalls = api.calls[n0:]
                jtokens = sum(int((c.get("usage") or {}).get("total_tokens") or 0) for c in jcalls)
                records["judges"][qid][model] = {
                    "verdict": verdict, "attempts": attempts,
                    "usage": {"calls": len(jcalls), "tokens": jtokens,
                              "unknown": sum(1 for c in jcalls if not (c.get("usage") or {}).get("total_tokens"))},
                    "missing": verdict is None,
                }
                save_results(records)
                log(f"judge {qid}/{model} -> {verdict}")

    # ---- 汇总 manifest（账本、代码版本、披露） ----
    from collections import defaultdict
    ledger_by_model = defaultdict(lambda: {"actual": 0, "unknown": 0, "in_flight": 0})
    lp = RUN / "ledger.jsonl"
    if lp.exists():
        rows = [json.loads(l) for l in lp.read_text(encoding="utf-8").splitlines() if l.strip()]
        settled = {r["id"] for r in rows if r["type"] in ("committed", "committed_unknown")}
        for r in rows:
            m = r.get("model", "?")
            if r["type"] == "committed":
                ledger_by_model[m]["actual"] += r.get("actual", 0)
            elif r["type"] == "committed_unknown":
                ledger_by_model[m]["unknown"] += r.get("estimate", 0)
            elif r["type"] == "reserved" and r["id"] not in settled:
                ledger_by_model[m]["in_flight"] += r.get("estimate", 0)  # 仅未结算预留
    counts = defaultdict(int)
    for rec in records["questions"].values():
        counts[rec.get("status", "?")] += 1
    judged = sum(1 for q in records["questions"] if records.get("judges", {}).get(q, {}).get(JUDGES[0], {}).get("verdict"))
    agree = sum(1 for q in records["questions"]
                if records.get("judges", {}).get(q, {}).get(JUDGES[0], {}).get("verdict") is not None
                and records["judges"][q].get(JUDGES[1], {}).get("verdict") is not None
                and records["judges"][q][JUDGES[0]]["verdict"].get("answer_correct")
                == records["judges"][q][JUDGES[1]]["verdict"].get("answer_correct"))
    both_judged = sum(1 for q in records["questions"]
                      if records.get("judges", {}).get(q, {}).get(JUDGES[0], {}).get("verdict") is not None
                      and records["judges"][q].get(JUDGES[1], {}).get("verdict") is not None)
    records["summary"] = {
        "status_counts": dict(counts), "completed": len(records["questions"]),
        "target": 43, "judged_main": judged, "both_judged": both_judged,
        "judge_agreement_on_answer_correct": f"{agree}/{both_judged}" if both_judged else "0/0",
    }
    records["manifest"] = {
        "commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip(),
        "frozen_scenarios_sha256": __import__("hashlib").sha256(
            (RUN / "scenarios_frozen.json").read_bytes()).hexdigest(),
        "corpus": {"docs": len(frozen["corpus_docs"]), "total_bytes": sum(
            (RUN / "corpus" / d["file"]).stat().st_size for d in frozen["corpus_docs"]),
            "label_stripped": True, "collection": "kb_qwen37_1024_v2", "tenant": "n31real"},
        "ledger_by_model": dict(ledger_by_model),
        "disclosures": [
            "rerank 停用：评测前探针缺陷（bm25 探针未摘除 reranker）误耗 p1 rerank 槽 97584 tokens（账本 real_run/ledger.jsonl 留痕），p1 剩余 5116 不足以支撑正式批；未获用户追加授权前不越过 p1，故本轮链路为 Embedding+Qdrant+BM25+RRF（无 Rerank）。语料已入库，若后续获授权可将 RERANK_ENABLED=true 断点重跑恢复真实 rerank",
            "探针事故与本轮停用决策属执行缺陷与预算受限，如实披露，不视为评测结论的一部分",
            "主裁判与生成模型同为 qwen3.8 系列（qwen3.8-max-0902），存在自评/同系列偏好风险",
            "双裁判一致率不是人工准确率；本题集未做人工复核（human_reviewed=false）",
            "缺判（裁判无有效 verdict）如实计 0 分并单独披露",
            "指标演示与工单流程不在本 43 题范围内，工作台单独展示，不代表 BGL 生产诊断能力",
        ],
    }
    save_results(records)
    log(f"ALL DONE: {records['summary']}")


if __name__ == "__main__":
    main()

"""n31real 重排恢复批（run-20261101）：真实 DashScope 重排 + 修复后行边界证据摘录。

与基线（eval/night20261031/real_run，重排停用）的关系：
- 题目、语料、租户、检索参数（hybrid/top5/chunk2000）、生成与裁判配置全部沿用基线；
- 差异仅两处：RERANK_ENABLED=true（qwen3.7-text-rerank 真实重排）；
  证据摘录为行边界安全版（修复基线 [:500] 硬截断把一行日志从中间切断的缺陷）；
- 规模由 p1 份额决定：test30 按 12 个家族分层抽样 20 题（≥2 题家族取前 2，单题家族取 1），
  reg3 作为整块门控（预算足够 3 题全跑才启动，否则整块标记 not_run 并披露）；
- 逐题预检：0902 余量不足则跳题；rerank 余量不足则该题停用重排并逐题标注。
- 独立运行记录（本目录 results.json / ledger），基线成绩原样保留；断点续跑按已完成题跳过。
"""
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
RUN = HERE / "rerank_run"
RUN.mkdir(parents=True, exist_ok=True)
ROOT = HERE.parents[1]
BASELINE = ROOT / "eval" / "night20261031" / "real_run"
LEDGER = HERE / "eval_ledger.jsonl"

sys.path.insert(0, str(HERE))
from p1_budget import effective_limits, round_status  # noqa: E402

MODEL_LIMITS, TOTAL_LIMIT = effective_limits(LEDGER, [HERE / "workbench" / "ledger.jsonl"])

os.environ["DATABASE_URL"] = "mysql+pymysql://aiops:aiops123@127.0.0.1:13306/aiops?charset=utf8mb4"
os.environ["QDRANT_MODE"] = "remote"
os.environ["QDRANT_URL"] = "http://127.0.0.1:6333"
os.environ["QDRANT_COLLECTION"] = "kb_qwen37_1024_v2"
os.environ["EMBEDDING_PROVIDER"] = "openai"
os.environ["EMBEDDING_DIM"] = "1024"
os.environ["EMBEDDING_MODEL"] = "qwen3.7-text-embedding-flash"
os.environ["LLM_PROVIDER"] = "openai"
os.environ["MODEL_STRICT"] = "true"
os.environ["CHAT_MODEL"] = "qwen3.8-max-0902"
os.environ["RERANK_ENABLED"] = "true"
os.environ["RERANK_PROVIDER"] = "dashscope"
os.environ["RERANK_MODEL"] = "qwen3.7-text-rerank"
os.environ["AGENT_MODE"] = "rule"
os.environ["BUDGET_LEDGER_PATH"] = str(LEDGER)
os.environ["BUDGET_TOKEN_LIMIT"] = str(TOTAL_LIMIT)
os.environ["BUDGET_MODEL_LIMITS"] = json.dumps(MODEL_LIMITS)

GEN_MODEL = "qwen3.8-max-0902"
JUDGES = ["qwen3.8-max-0902", "qwen3.8-27b"]
CHUNK_SIZE = 2000

# 逐题保守成本预估（依据基线实测：grade≈1,400、generate≈2,900、judge≈2,900、rerank≈6,600/轮）
EST = {"gen_0902": 4700, "judge_0902": 2900, "judge_27b": 2900, "rerank_round": 6600, "embedding": 150}

sys.path.insert(0, str(ROOT))


def log(m):
    print(f"[{datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S')}] {m}", flush=True)


def stratified_sample(scenarios, n_per_family=2):
    """test30 家族分层抽样：≥n 题家族按 ID 升序取前 n，单题家族取 1，ID 交错排序保证均衡覆盖。"""
    by_family = defaultdict(list)
    for s in scenarios:
        if s["split"] == "test":
            by_family[s["family"]].append(s)
    picked = []
    for fam in sorted(by_family):
        picked.extend(sorted(by_family[fam], key=lambda s: s["id"])[:n_per_family])
    # 家族轮转交错：同一家族的题不相邻，中途预算耗尽也保持家族覆盖
    picked.sort(key=lambda s: (s["family"], s["id"]))
    rounds = []
    from itertools import groupby
    for _, grp in groupby(picked, key=lambda s: s["family"]):
        rounds.append(list(grp))
    ordered = []
    for i in range(max(len(g) for g in rounds)):
        for g in rounds:
            if i < len(g):
                ordered.append(g[i])
    return ordered


def load_results():
    fp = RUN / "results.json"
    if fp.exists():
        return json.loads(fp.read_text(encoding="utf-8"))
    return {"namespace": "n31real", "batch": "rerank_restore_20", "questions": {}, "judges": {}, "config": {}}


def save_results(records):
    (RUN / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")


def merged_remaining():
    st = round_status([LEDGER, HERE / "workbench" / "ledger.jsonl"])
    return st["models_remaining"]


def usage_delta(container, marks):
    out = {}
    for key, obj, attr in [("embedding", container.embedder, "calls"),
                           ("rerank", container.retriever.reranker, "calls"),
                           ("chat", container.llm._api, "calls")]:
        if obj is None or not hasattr(obj, attr):
            continue
        for rec in getattr(obj, attr)[marks[key]:]:
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


JUDGE_SYSTEM = (
    "你是评测裁判。仅依据题目、金标要点、rubric 与编号证据评判该回答，"
    "不要使用证据之外的知识。只输出 JSON："
    "{\"answer_correct\":bool,\"grounded\":bool,\"appropriate_abstention\":bool,\"reason\":\"≤120字理由\"}。"
    "注意：若金标要点本身要求“明确说明不存在/无法断定”，回答做到即算 appropriate_abstention=true。")
RUBRIC = {"answer_correct": "回答是否覆盖金标要点且无事实错误（含计数/行号/时间戳/节点）",
          "grounded": "回答是否仅依据给出的编号证据，无编造（引用可定位到证据编号）",
          "appropriate_abstention": "对无法从证据得知的问题（含 must_abstain 题）是否拒绝编造并说明依据不足"}


def judge_payload(rec):
    return json.dumps({
        "question": rec["question"], "required_points": rec["required_points"],
        "must_abstain": rec["must_abstain"], "rubric": RUBRIC,
        "answer": rec.get("answer", ""), "evidence": rec.get("evidence", []),
        "generation_succeeded": rec.get("status") == "answered",
    }, ensure_ascii=False)


def main():
    from fastapi.testclient import TestClient

    from app.main import app

    frozen = json.loads((BASELINE / "scenarios_frozen.json").read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in frozen["scenarios"]}
    test_order = stratified_sample(frozen["scenarios"])
    reg_block = [by_id[i] for i in ("reg-001", "reg-002", "reg-003")]
    records = load_results()
    records["config"] = {
        "generation": GEN_MODEL, "judges": JUDGES,
        "embedding": "qwen3.7-text-embedding-flash", "rerank": "qwen3.7-text-rerank（真实重排，本轮授权恢复）",
        "retrieval": {"mode": "hybrid", "collection": "kb_qwen37_1024_v2", "tenant": "n31real", "dim": 1024,
                      "top_k": 5, "chunk_size": CHUNK_SIZE,
                      "isolation_note": "复用基线语料与向量（租户 n31real），不重复入库"},
        "agent_mode": "rule", "temperature": 0, "max_tokens": 1536,
        "evidence_excerpt": "行边界安全摘录 ≤500 字符（app.agent.llm.evidence_excerpt）；基线为硬 [:500] 截断",
        "budget": {"total_limit": TOTAL_LIMIT, "model_limits": MODEL_LIMITS,
                   "derivation": "budget.json p1 份额 − 兄弟账本已用（合并核算，见 eval/night20261101/p1_budget.py）",
                   "ledger": str(LEDGER)},
        "concurrency": 1, "max_extra_retries": 1,
        "scope": "test30 家族分层抽样 20 题（每家族按 ID 升序取前 2，单题家族取 1）+ reg3 整块门控；"
                 "dev10 不在本批（0902 份额有限，基线已完整覆盖 43 题）",
    }

    with TestClient(app) as client:
        from app.auth.security import hash_password
        from app.container import get_container
        from app.db.base import SessionLocal
        from app.db.models import Tenant, User

        with SessionLocal() as db:
            if not db.get(Tenant, "n31real"):
                db.add(Tenant(id="n31real", name="n31real BGL 真实日志评测租户"))
            if not db.query(User).filter(User.username == "n31real_admin").first():
                db.add(User(tenant_id="n31real", username="n31real_admin",
                            password_hash=hash_password("n31admin-pass"), role="admin"))
            db.commit()
        login = client.post("/auth/login", json={"username": "n31real_admin", "password": "n31admin-pass"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        container = get_container()
        assert container.retriever.reranker is not None, "重排未启用，本批不得在无重排下静默运行"

        # 语料入库（幂等：基线已入库则跳过；租户隔离，绝不全租户回填）
        with SessionLocal() as db:
            from app.db.models import Document
            existing = {d.title for d in db.query(Document).filter(Document.tenant_id == "n31real").all()}
            for doc_meta in frozen["corpus_docs"]:
                if doc_meta["title"] in existing:
                    continue
                text = (BASELINE / "corpus" / doc_meta["file"]).read_text(encoding="utf-8")
                doc = Document(tenant_id="n31real", title=doc_meta["title"], content=text,
                               source="BGL.zip/BGL.log", created_by="n31real_admin")
                from app.services import kb_service
                n = kb_service.ingest_document(db, container, doc, chunk_size=CHUNK_SIZE)
                log(f"ingested {doc_meta['title']}: {n} chunks")

        def run_question(s, allow_rerank=True):
            qid = s["id"]
            t0 = time.time()
            sid = client.post("/api/sessions", json={"title": f"n31rr-{qid}"}, headers=headers).json()["id"]
            marks = mark(container)
            saved_reranker = container.retriever.reranker
            if not allow_rerank:
                container.retriever.reranker = None  # 该题停用重排（预检门控），逐题标注
            attempts, rec = [], None
            try:
                for attempt in (1, 2):
                    try:
                        r = client.post("/api/agent/chat",
                                        json={"session_id": sid, "message": s["question"]}, headers=headers)
                        body = r.json()
                        events = body.get("events", [])
                        retrievals = [e for e in events if e.get("type") == "retrieval"]
                        final_hits = retrievals[-1]["hits"] if retrievals else []
                        from app.agent.llm import evidence_excerpt
                        status = body.get("status") if body.get("status") in ("answered", "approval_required") \
                            else f"abnormal:{body.get('status')}"
                        rec = {
                            "status": status if r.status_code == 200 and body.get("answer") is not None else f"http_{r.status_code}",
                            "question": s["question"], "split": s["split"], "family": s["family"],
                            "window": s["window"], "must_abstain": s["must_abstain"],
                            "required_points": s["required_points"],
                            "rerank_active": allow_rerank and bool(retrievals and retrievals[-1].get("reranked")),
                            "answer": body.get("answer", ""),
                            "end_reason": body.get("end_reason"),
                            "citations": body.get("citations", []),
                            "evidence": [{"n": i + 1, "doc_title": h.get("doc_title"), "chunk_id": h.get("chunk_id"),
                                          "score": h.get("score"), "text": evidence_excerpt(h.get("text") or "", 500)}
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
                                   "evidence": [], "rerank_active": False, "error": attempts,
                                   "elapsed_s": round(time.time() - t0, 1), "attempts": attempts, "session_id": sid}
            finally:
                container.retriever.reranker = saved_reranker
            rec["usage"] = usage_delta(container, marks)
            records["questions"][qid] = rec
            save_results(records)
            log(f"{qid} done status={rec['status']} rerank={rec.get('rerank_active')} "
                f"{rec['elapsed_s']}s usage={ {m: u['tokens'] for m, u in rec['usage'].items()} }")

        def run_judges(qid):
            rec = records["questions"].get(qid)
            if not rec or (rec.get("status") != "answered" and not rec.get("answer")):
                return
            records["judges"].setdefault(qid, {})
            payload = judge_payload(rec)
            from app.config import get_settings
            from app.services.model_api import ChatAPI
            settings = get_settings()
            for model in JUDGES:
                if records["judges"][qid].get(model, {}).get("verdict"):
                    continue
                api = ChatAPI(settings.openai_api_key, settings.openai_base_url, model)
                attempts, verdict, n0 = [], None, len(api.calls)
                for attempt in (1, 2):
                    try:
                        verdict = json.loads(api.chat(JUDGE_SYSTEM, payload, json_mode=True))
                        break
                    except Exception as exc:
                        attempts.append({"attempt": attempt, "error_type": type(exc).__name__, "message": str(exc)[:200]})
                        log(f"judge {qid}/{model} attempt {attempt} failed: {type(exc).__name__}")
                jcalls = api.calls[n0:]
                records["judges"][qid][model] = {
                    "verdict": verdict, "attempts": attempts,
                    "usage": {"calls": len(jcalls),
                              "tokens": sum(int((c.get("usage") or {}).get("total_tokens") or 0) for c in jcalls),
                              "unknown": sum(1 for c in jcalls if not (c.get("usage") or {}).get("total_tokens"))},
                    "missing": verdict is None,
                }
                save_results(records)
                log(f"judge {qid}/{model} -> {verdict}")

        # ---- test 分层抽样批：逐题预检门控 ----
        skipped = []
        for s in test_order:
            qid = s["id"]
            if records["questions"].get(qid, {}).get("status") not in (None, "pending", "not_run"):
                run_judges(qid)
                continue
            rem = merged_remaining()
            if rem.get(GEN_MODEL, 0) < EST["gen_0902"] + EST["judge_0902"] or \
               rem.get("qwen3.8-27b", 0) < EST["judge_27b"] or rem.get("qwen3.7-text-embedding-flash", 0) < EST["embedding"]:
                skipped.append({"id": qid, "reason": "budget_preflight_refused",
                                "remaining": {k: rem.get(k) for k in (GEN_MODEL, "qwen3.8-27b", "qwen3.7-text-rerank")}})
                records.setdefault("not_run", {})[qid] = skipped[-1]
                save_results(records)
                log(f"{qid} SKIPPED (budget preflight)")
                continue
            allow_rerank = rem.get("qwen3.7-text-rerank", 0) >= EST["rerank_round"]
            run_question(s, allow_rerank=allow_rerank)
            run_judges(qid)

        # ---- reg3 整块门控：预算足够 3 题全跑才启动，否则整块 not_run ----
        if all(records["questions"].get(s["id"], {}).get("status") in (None, "pending", "not_run") for s in reg_block):
            rem = merged_remaining()
            need = {"0902": 3 * (EST["gen_0902"] + EST["judge_0902"]), "27b": 3 * EST["judge_27b"],
                    "rerank": 3 * EST["rerank_round"]}
            if rem.get(GEN_MODEL, 0) >= need["0902"] and rem.get("qwen3.8-27b", 0) >= need["27b"] \
                    and rem.get("qwen3.7-text-rerank", 0) >= need["rerank"]:
                for s in reg_block:
                    run_question(s, allow_rerank=True)
                    run_judges(s["id"])
            else:
                for s in reg_block:
                    records.setdefault("not_run", {})[s["id"]] = {
                        "id": s["id"], "reason": "reg_block_budget_preflight_refused",
                        "needed": need, "remaining": {k: rem.get(k) for k in (GEN_MODEL, "qwen3.8-27b", "qwen3.7-text-rerank")}}
                save_results(records)
                log(f"reg3 block SKIPPED (budget preflight): need={need} remaining={ {k: rem.get(k) for k in (GEN_MODEL, 'qwen3.8-27b', 'qwen3.7-text-rerank')} }")

    # ---- 汇总与 manifest ----
    rows = [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()] if LEDGER.exists() else []
    settled = {r["id"] for r in rows if r["type"] in ("committed", "committed_unknown")}
    ledger_by_model = defaultdict(lambda: {"actual": 0, "unknown": 0, "in_flight": 0})
    for r in rows:
        m = r.get("model", "?")
        if r["type"] == "committed":
            ledger_by_model[m]["actual"] += r.get("actual", 0)
        elif r["type"] == "committed_unknown":
            ledger_by_model[m]["unknown"] += r.get("estimate", 0)
        elif r["type"] == "reserved" and r["id"] not in settled:
            ledger_by_model[m]["in_flight"] += r.get("estimate", 0)
    counts = defaultdict(int)
    for rec in records["questions"].values():
        counts[rec.get("status", "?")] += 1
    judged = sum(1 for q in records["questions"] if records.get("judges", {}).get(q, {}).get(JUDGES[0], {}).get("verdict"))
    both = sum(1 for q in records["questions"]
               if records.get("judges", {}).get(q, {}).get(JUDGES[0], {}).get("verdict") is not None
               and records["judges"][q].get(JUDGES[1], {}).get("verdict") is not None)
    agree = sum(1 for q in records["questions"]
                if records.get("judges", {}).get(q, {}).get(JUDGES[0], {}).get("verdict") is not None
                and records["judges"][q].get(JUDGES[1], {}).get("verdict") is not None
                and records["judges"][q][JUDGES[0]]["verdict"].get("answer_correct")
                == records["judges"][q][JUDGES[1]]["verdict"].get("answer_correct"))
    records["summary"] = {"status_counts": dict(counts), "completed": len(records["questions"]),
                          "planned": len(test_order) + 3, "judged_main": judged, "both_judged": both,
                          "judge_agreement_on_answer_correct": f"{agree}/{both}" if both else "0/0",
                          "not_run": sorted(records.get("not_run", {}))}
    reranked_n = sum(1 for rec in records["questions"].values() if rec.get("rerank_active"))
    records["manifest"] = {
        "commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip(),
        "baseline": {"results": str(BASELINE / "results.json"), "rerank": "disabled（历史批，成绩原样保留）"},
        "sample": {"stratified_test20": [s["id"] for s in test_order],
                   "families": sorted({s["family"] for s in test_order}),
                   "reg_block_policy": "整块门控（3 题预算足够才跑）"},
        "rerank_coverage": {"questions_reranked": reranked_n, "questions_total": len(records["questions"])},
        "ledger_by_model": dict(ledger_by_model),
        "disclosures": [
            "本批为重排恢复批（独立运行记录）：与基线唯一链路差异 = 真实 DashScope 重排 + 行边界安全证据摘录；基线成绩保留不覆盖",
            "dev10 未纳入本批（0902 份额有限）；reg3 按整块门控决定是否运行，未跑则如实标 not_run",
            "若逐题 rerank_active=false：该题因 rerank 预检余量不足停用重排，仍如实入结果并单列",
            "主裁判与生成模型同为 qwen3.8 系列（qwen3.8-max-0902），存在自评/同系列偏好风险",
            "双裁判一致率不是人工准确率；本题集未做人工复核（human_reviewed=false）",
            "分层抽样结果不代表 test30 全集分数；全集结论以基线 43 题为准",
            "test-019 首次生成成功但产物目录缺失未能落盘，重启后整题重跑一次（基础设施重试，两次用量均已在账本留痕）",
        ],
    }
    save_results(records)
    log(f"ALL DONE: {records['summary']}")


if __name__ == "__main__":
    main()

"""终审整改真实验证批（run-20261101-p1-final-01 → P1-EVIDENCE/P1-GOLD 验收）。

范围（预先固定）：重排批中双裁判均判 answer_correct=False 的 12 题
（test-021/009/004/005/001/015/017/023/006 + reg-001/002/003），以整改后链路重跑：
真实 DashScope 重排 + 证据组装（行范围完整摘录置首、块首残行剥离、行边界收口）+
范围完整性提示词防则；test-004 使用 gold_v2 更正后题干。双裁判判分，裁判看到
生成器实际收到的同一编号证据（used_evidence）。
独立运行记录（本目录），旧成绩原样保留；断点续跑按已完成题跳过。
追加授权：2026-11-01 用户会话内"全额授权"（0902 +130K / 27b +15K / rerank +65K）。
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
JUDGES = (sys.argv[4].split(",") if len(sys.argv) > 4 and sys.argv[4]
          else ["qwen3.8-max-0902", "qwen3.8-27b"])
CHUNK_SIZE = 2000

# 用法：run_review_fix_verify.py [输出目录名] [逗号分隔全跑题id] [逗号分隔仅补判题id] [逗号分隔裁判模型]
# 缺省 = 整改验证批（p1-final-01 范围）。p1-final-02 定向批：
#   全跑 = 受残行影响 6 题 + reg-002/003；仅补判 = reg-001(27b) / test-030(0902+27b)。
# P1-ARAG-20261102 定向批：verify_stats / test-005 / 无补判 / deepseek 单裁判（0902 余量不足以承担主判）。
RUN_NAME = sys.argv[1] if len(sys.argv) > 1 else "verify_run"
SCOPE = (sys.argv[2].split(",") if len(sys.argv) > 2 else
         ["test-021", "test-009", "test-004", "test-005", "test-001",
          "test-015", "test-017", "test-023", "test-006", "reg-001", "reg-002", "reg-003", "test-030"])
JUDGE_ONLY = set(sys.argv[3].split(",")) if len(sys.argv) > 3 and sys.argv[3] else set()


def reserve_estimates(question: str) -> dict:
    """按账本同口径（字节上界预留）估算本题生成+裁判的预留需求，门控与账本一致不再半途拒答。"""
    from app.rag.evidence import parse_line_targets

    range_chars = min(12000, max(200, len(parse_line_targets(question)) * 160))
    gen_reserve = 3400 + range_chars          # 生成调用预留上界（载荷+包装+输出1536）
    judge_reserve = 3700 + range_chars        # 裁判调用预留上界（同证据+判分输出）
    return {"gen_0902": gen_reserve, "judge_0902": judge_reserve, "judge_27b": judge_reserve,
            "rerank_round": 9800, "embedding": 200}


RUN = HERE / RUN_NAME
RUN.mkdir(parents=True, exist_ok=True)
PREV_RUN = HERE / "verify_run"  # 上一批记录（仅补判题的既有答案来源）

sys.path.insert(0, str(ROOT))


def log(m):
    print(f"[{datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S')}] {m}", flush=True)


def load_results():
    fp = RUN / "results.json"
    if fp.exists():
        return json.loads(fp.read_text(encoding="utf-8"))
    return {"namespace": "n31real", "batch": f"review_fix_{RUN_NAME}", "questions": {}, "judges": {}, "not_run": {}}


def save_results(records):
    (RUN / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")


def merged_remaining():
    return round_status([LEDGER, HERE / "workbench" / "ledger.jsonl"])["models_remaining"]


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


def judge_payload(rec, corrected_points=None, note=None):
    points = corrected_points if corrected_points is not None else rec["required_points"]
    body = {"question": rec["question"], "required_points": points,
            "must_abstain": rec["must_abstain"], "rubric": RUBRIC,
            "answer": rec.get("answer", ""), "evidence": rec.get("evidence", []),
            "generation_succeeded": rec.get("status") == "answered"}
    if rec.get("range_stats") is not None:
        # 裁判必须拿到生成器实际收到的统计工具输出（与证据同源，不额外提供金标）
        body["range_stats"] = rec.get("range_stats")
    if note:
        body["gold_correction_note"] = note
    return json.dumps(body, ensure_ascii=False)


def main():
    from fastapi.testclient import TestClient

    from app.main import app

    frozen = json.loads((BASELINE / "scenarios_frozen.json").read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in frozen["scenarios"]}
    corr = json.loads((HERE / "gold_corrections" / "corrections.json").read_text(encoding="utf-8"))
    cmap = {c["id"]: c for c in corr["corrections"]}
    records = load_results()
    records["config"] = {
        "generation": GEN_MODEL, "judges": JUDGES,
        "embedding": "qwen3.7-text-embedding-flash", "rerank": "qwen3.7-text-rerank（真实重排）",
        "retrieval": {"mode": "hybrid", "collection": "kb_qwen37_1024_v2", "tenant": "n31real", "dim": 1024,
                      "top_k": 5, "chunk_size": CHUNK_SIZE, "isolation_note": "复用既有语料与向量，不重复入库"},
        "agent_mode": "rule", "temperature": 0, "max_tokens": 1536,
        "evidence": "app.rag.evidence 组装：显式行范围题完整行摘录置首（range_assembly）+ 分块摘录行边界安全（块首残行剥离）；"
                    "生成提示词含范围完整性防则；裁判与评测记录使用生成器实际收到的 used_evidence",
        "budget": {"total_limit": TOTAL_LIMIT, "model_limits": MODEL_LIMITS,
                   "derivation": "budget.json p1 + 2026-11-01 用户两次追加授权（整改验证）− 兄弟账本已用",
                   "ledger": str(LEDGER)},
        "scope": f"全跑={SCOPE}；仅补判={sorted(JUDGE_ONLY)}（既有答案来自上一批，不重新生成）；"
                 "test-004/test-030 金标按 gold_v2 更正后要点判分",
        "concurrency": 1, "max_extra_retries": 1,
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
        assert container.retriever.reranker is not None, "重排未启用"

        from app.config import get_settings
        settings = get_settings()
        from app.services.model_api import ChatAPI

        # 仅补判题：既有答案与证据来自上一批（保留旧记录，不重新生成）
        prev = json.loads((PREV_RUN / "results.json").read_text(encoding="utf-8")) \
            if (PREV_RUN / "results.json").exists() else {"questions": {}, "judges": {}}
        for qid in sorted(JUDGE_ONLY):
            if qid in records["questions"]:
                continue
            src = prev["questions"].get(qid)
            if src is None:
                log(f"{qid} judge-only: 上一批无记录，跳过")
                continue
            records["questions"][qid] = dict(src)
            records["judges"].setdefault(qid, dict(prev.get("judges", {}).get(qid, {})))
            save_results(records)
            log(f"{qid} judge-only seeded from {PREV_RUN.name}")

        for qid in list(SCOPE) + sorted(JUDGE_ONLY):
            if qid in JUDGE_ONLY:
                cmap_note = cmap.get(qid)
                corrected = cmap_note["new"]["required_points"] if cmap_note else None
                note = f"金标已按原始日志更正（{corr['version']}）：{cmap_note['reason']}" if cmap_note else None
                run_judges(records, qid, corrected, note, settings)
                continue
            s = dict(by_id[qid])
            note = None
            corrected_points = None
            if qid in cmap:
                c = cmap[qid]
                s["question"] = c["new"]["question"]
                corrected_points = c["new"]["required_points"]
                s["required_points"] = corrected_points
                note = f"金标已按原始日志更正（{corr['version']}）：{c['reason']}"
            if records["questions"].get(qid, {}).get("status") not in (None, "pending", "not_run"):
                run_judges(records, qid, corrected_points, note, settings)
                continue
            rem = merged_remaining()
            est = reserve_estimates(s["question"])
            judge_need = {m: (est["judge_0902"] if m == GEN_MODEL else est["judge_27b"]) for m in JUDGES}
            if rem.get(GEN_MODEL, 0) < est["gen_0902"] + judge_need.get(GEN_MODEL, 0) or \
               any(rem.get(m, 0) < need for m, need in judge_need.items() if m != GEN_MODEL) or \
               rem.get("qwen3.7-text-embedding-flash", 0) < est["embedding"]:
                records["not_run"][qid] = {"reason": "budget_preflight_refused",
                                           "needed": est,
                                           "remaining": {k: rem.get(k) for k in (GEN_MODEL, "qwen3.8-27b", "qwen3.7-text-rerank")}}
                save_results(records)
                log(f"{qid} SKIPPED (budget preflight, reserve-based)")
                continue
            allow_rerank = rem.get("qwen3.7-text-rerank", 0) >= est["rerank_round"]
            if not allow_rerank:
                log(f"{qid} rerank disabled by budget (remaining {rem.get('qwen3.7-text-rerank')} < reserve {est['rerank_round']})，该题无重排并逐题标注")
            t0 = time.time()
            sid = client.post("/api/sessions", json={"title": f"n31fix-{qid}"}, headers=headers).json()["id"]
            marks = mark(container)
            saved_reranker = container.retriever.reranker
            if not allow_rerank:
                container.retriever.reranker = None  # 预算不足，该题停用重排并逐题标注
            attempts, rec = [], None
            try:
                for attempt in (1, 2):
                    try:
                        r = client.post("/api/agent/chat", json={"session_id": sid, "message": s["question"]}, headers=headers)
                        body = r.json()
                        events = body.get("events", [])
                        answers = [e for e in events if e.get("type") == "answer"]
                        used = answers[-1].get("used_evidence") if answers else None
                        retrievals = [e for e in events if e.get("type") == "retrieval"]
                        if not used:
                            final_hits = retrievals[-1]["hits"] if retrievals else []
                            from app.rag.evidence import assemble_evidence
                            with SessionLocal() as db:
                                used, _ = assemble_evidence(final_hits[:5], s["question"], db, "n31real")
                        status = body.get("status") if body.get("status") in ("answered", "approval_required") else f"abnormal:{body.get('status')}"
                        rec = {
                            "status": status if r.status_code == 200 and body.get("answer") is not None else f"http_{r.status_code}",
                            "question": s["question"], "split": s["split"], "family": s["family"],
                            "window": s["window"], "must_abstain": s["must_abstain"],
                            "required_points": s["required_points"],
                            "gold_corrected": qid in cmap,
                            "rerank_active": allow_rerank and bool(retrievals and retrievals[-1].get("reranked")),
                            "answer": body.get("answer", ""),
                            "end_reason": body.get("end_reason"),
                            "citations": body.get("citations", []),
                            "citations_indexed": body.get("citations_indexed", []),
                            "evidence": used,
                            "range_assembly": body.get("range_assembly"),
                            "range_stats": body.get("range_stats"),
                            "range_coverage": body.get("range_coverage"),
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
                                   "required_points": s["required_points"], "gold_corrected": qid in cmap,
                                   "rerank_active": False,
                                   "answer": "", "citations": [], "citations_indexed": [], "evidence": [],
                                   "error": attempts, "elapsed_s": round(time.time() - t0, 1),
                                   "attempts": attempts, "session_id": sid}
            finally:
                container.retriever.reranker = saved_reranker
            rec["usage"] = usage_delta(container, marks)
            records["questions"][qid] = rec
            records.get("not_run", {}).pop(qid, None)
            save_results(records)
            log(f"{qid} done status={rec['status']} rounds={rec.get('retrieval_rounds')} "
                f"{rec['elapsed_s']}s usage={ {m: u['tokens'] for m, u in rec['usage'].items()} }")
            run_judges(records, qid, corrected_points, note, settings)

        # ---- 27b 判分缺失的题：DeepSeek 跨家族复核作第三视角（deepseek 份额内，单独存放） ----
        run_cross_reviews(container, records, settings)

    summarize(records)


def run_cross_reviews(container, records, settings):
    from app.services.model_api import ChatAPI

    CROSS = "deepseek-v4-pro-0813"
    records.setdefault("cross_reviews", {})
    for qid in SCOPE:
        rec = records["questions"].get(qid)
        j = records.get("judges", {}).get(qid, {})
        if not rec or rec.get("status") != "answered":
            continue
        if j.get(JUDGES[1], {}).get("verdict") and j.get(JUDGES[0], {}).get("verdict"):
            continue  # 双裁判齐全，无需复核
        if records["cross_reviews"].get(qid, {}).get(CROSS, {}).get("verdict"):
            continue
        api = ChatAPI(settings.openai_api_key, settings.openai_base_url, CROSS)
        payload = judge_payload(rec)
        attempts, verdict, n0 = [], None, len(api.calls)
        for attempt in (1, 2):
            try:
                verdict = json.loads(api.chat(JUDGE_SYSTEM, payload, json_mode=True))
                break
            except Exception as exc:
                attempts.append({"attempt": attempt, "error_type": type(exc).__name__, "message": str(exc)[:200]})
                log(f"cross {qid}/{CROSS} attempt {attempt} failed: {type(exc).__name__}")
        jcalls = api.calls[n0:]
        records["cross_reviews"][qid] = {CROSS: {
            "verdict": verdict, "attempts": attempts,
            "usage": {"calls": len(jcalls),
                      "tokens": sum(int((c.get("usage") or {}).get("total_tokens") or 0) for c in jcalls),
                      "unknown": sum(1 for c in jcalls if not (c.get("usage") or {}).get("total_tokens"))},
            "missing": verdict is None,
            "role": "cross_family_missing_second_judge",
            "note": "27b 判分因预算预留被拒（额度不足）时的跨家族第三视角；不冒充第二裁判，缺判如实披露",
        }}
        save_results(records)
        log(f"cross {qid}/{CROSS} -> {verdict}")


def run_judges(records, qid, corrected_points, note, settings):
    from app.services.model_api import ChatAPI

    rec = records["questions"].get(qid)
    if not rec or (rec.get("status") != "answered" and not rec.get("answer")):
        return
    records["judges"].setdefault(qid, {})
    payload = judge_payload(rec, corrected_points, note)
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


def summarize(records):
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
                          "planned": len(set(SCOPE) | JUDGE_ONLY), "judged_main": judged, "both_judged": both,
                          "judge_agreement_on_answer_correct": f"{agree}/{both}" if both else "0/0",
                          "not_run": sorted(records.get("not_run", {}))}
    records["manifest"] = {
        "commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip(),
        "authorization": "本轮用户明确追加授权见 p1_budget.REVIEW_FIX_GRANTS；所有调用合并计入本轮账本，未授权额度不得动用",
        "scope": {"regenerated": SCOPE, "judge_only": sorted(JUDGE_ONLY),
                  "gold_corrected": sorted(q for q, rec in records["questions"].items() if rec.get("gold_corrected"))},
        "ledger_by_model": dict(ledger_by_model),
        "cross_reviews": {q: bool(v.get("deepseek-v4-pro-0813", {}).get("verdict"))
                          for q, v in records.get("cross_reviews", {}).items()},
        "disclosures": [
            "本批为终审整改验证：证据组装（范围完整摘录/块首残行剥离/行边界收口）+ 范围完整性提示词防则后的重跑",
            "金标更正仅适用于 scope.gold_corrected 列出的本批题目；旧成绩原样保留不覆盖",
            "scope.judge_only 复用上一批答案与同一编号证据，仅补缺判，不重新生成",
            "失败与缺判以本批逐题记录和 summary 为准；DeepSeek 仅为第三视角，不冒充双裁判",
            "双裁判为模型自动评分，未做人工复核（human_reviewed=false）",
        ],
    }
    save_results(records)
    log(f"ALL DONE: {records['summary']}")


if __name__ == "__main__":
    main()

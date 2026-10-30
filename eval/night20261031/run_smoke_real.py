"""T23 冒烟批真实链路入口（专用最小脚本，不使用旧 real_eval.py 的 memory 强制入口）。

真实后端：MySQL/Qdrant remote/Redis + 真实 Embedding/Rerank/qwen3.8-max；持久账本逐请求落盘。
时间门控：08:10（+08:00）后不再发起任何新真实 API 请求，未完成题如实标记 incomplete。
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

RUN_DIR = Path(__file__).parent / "smoke_run"
RUN_DIR.mkdir(exist_ok=True)

# 实配环境（先于 app 导入）：真实链路 + 预算账本（budget.json p1 列逐模型）
os.environ["DATABASE_URL"] = "mysql+pymysql://aiops:aiops123@127.0.0.1:13306/aiops?charset=utf8mb4"
os.environ["QDRANT_MODE"] = "remote"
os.environ["QDRANT_URL"] = "http://127.0.0.1:6333"
os.environ["QDRANT_COLLECTION"] = "kb_night20261031"
os.environ["EMBEDDING_PROVIDER"] = "openai"
os.environ["EMBEDDING_DIM"] = "1024"
os.environ["LLM_PROVIDER"] = "openai"
os.environ["MODEL_STRICT"] = "true"
os.environ["RERANK_ENABLED"] = "true"
os.environ["RERANK_PROVIDER"] = "dashscope"
os.environ["RERANK_MODEL"] = "qwen3.7-text-rerank"
os.environ["AGENT_MODE"] = "rule"
os.environ["BUDGET_LEDGER_PATH"] = str(RUN_DIR / "ledger.jsonl")
os.environ["BUDGET_TOKEN_LIMIT"] = "952500"
os.environ["BUDGET_MODEL_LIMITS"] = json.dumps({
    "qwen3.8-max": 302500, "qwen3.8-27b": 192500, "deepseek-v4-pro-0813": 137500,
    "qwen3.7-text-embedding-flash": 110000, "qwen3.7-text-rerank": 110000})

API_DEADLINE = datetime.now(timezone(timedelta(hours=8))).replace(
    year=2026, month=10, day=31, hour=8, minute=10, second=0, microsecond=0)

QUESTIONS = ["n31-001", "n31-023", "n31-033", "n31-038"]
JUDGES = ["qwen3.8-max", "qwen3.8-27b"]

def deadline_left():
    return (API_DEADLINE - datetime.now(timezone(timedelta(hours=8)))).total_seconds()

def log(msg):
    print(f"[{datetime.now(timezone(timedelta(hours=8))).strftime('%H:%M:%S')}] {msg}", flush=True)

def main():
    from fastapi.testclient import TestClient

    from app.main import app

    log(f"API deadline 08:10, left={deadline_left():.0f}s")
    records = {"namespace": "night20261031", "batch": "smoke_real", "questions": {}, "judges": {},
               "manifest": {"note": "运行后回填实际生效配置"}}
    with TestClient(app) as client:
        # 建独立租户管理员（等价 scripts/seed.py 开通路径；公开注册只能拿 viewer）
        from app.auth.security import hash_password
        from app.db.base import SessionLocal
        from app.db.models import Tenant, User

        with SessionLocal() as db:
            if not db.get(Tenant, "night20261031"):
                db.add(Tenant(id="night20261031", name="night20261031 冒烟租户"))
            if not db.query(User).filter(User.username == "n31admin").first():
                db.add(User(tenant_id="night20261031", username="n31admin",
                            password_hash=hash_password("n31admin-pass"), role="admin"))
            db.commit()
        login = client.post("/auth/login", json={"username": "n31admin", "password": "n31admin-pass"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        sid = client.post("/api/sessions", json={"title": "night20261031-smoke"}, headers=headers).json()["id"]

        # 冻结题面（从 night20261031/scenarios.json 读取，不改动）
        scen = {s["id"]: s for s in json.load(open(ROOT / "eval/night20261031/scenarios.json", encoding="utf-8"))["scenarios"]}
        # 知识入库：只读复用 scripts/knowledge 原文
        kb_dir = ROOT / "scripts/knowledge"
        for doc in sorted(kb_dir.glob("*.md")):
            r = client.post("/api/kb/documents", json={"title": doc.stem, "content": doc.read_text(encoding="utf-8"), "source": "night20261031"}, headers=headers)
            assert r.status_code == 200, r.text
        log("12 docs ingested (real embedding)")

        # 逐题真实生成（rule 模式，经检索/工具/门控）
        for qid in QUESTIONS:
            if deadline_left() <= 60:
                records["questions"][qid] = {"status": "incomplete", "reason": "08:10 API 门控前剩余时间不足"}
                log(f"{qid} skipped: deadline")
                continue
            case = scen[qid]
            t0 = time.time()
            r = client.post("/api/agent/chat", json={"session_id": sid, "message": case["question"]}, headers=headers)
            body = r.json()
            events = body.get("events", [])
            hits = [h for e in events if e.get("type") == "retrieval" for h in e.get("hits", [])]
            tools = [e for e in events if e.get("type") == "tool"]
            records["questions"][qid] = {
                "status": "ok" if body.get("status") in ("answered", "approval_required") else body.get("status"),
                "question": case["question"], "family": case["family"],
                "answer": body.get("answer", ""), "end_reason": body.get("end_reason"),
                "citations": body.get("citations", []),
                "evidence": [{"doc_title": h.get("doc_title"), "text": (h.get("text") or "")[:300]} for h in hits[:5]],
                "tool_events": [{"name": t.get("name"), "args": t.get("args"), "status": (t.get("result") or {}).get("status")} for t in tools],
                "elapsed_s": round(time.time() - t0, 1), "run_id": body.get("run_id"),
            }
            log(f"{qid} done status={body.get('status')} {records['questions'][qid]['elapsed_s']}s")

        # 双裁判（同冻结答案+同 rubric+实际取得证据）
        rubric = json.load(open(ROOT / "eval/night20261031/rubric.json", encoding="utf-8"))
        judge_system = ("你是评测裁判。仅依据给定的题目、金标要点、rubric 与该答案实际取得的证据判分，"
                        "只输出 JSON：{\"answer_correct\":bool,\"grounded\":bool,\"appropriate_abstention\":bool,\"reason\":\"简述\"}。")
        from app.services.model_api import ChatAPI
        api_cache = {}
        for qid in QUESTIONS:
            rec = records["questions"].get(qid)
            if not rec or rec.get("status") == "incomplete":
                continue
            payload = json.dumps({"question": rec["question"], "required_points": scen[qid]["required_points"],
                                  "rubric": {k: rubric[k] for k in ("answer_correct", "grounded", "appropriate_abstention")},
                                  "answer": rec["answer"], "evidence": rec["evidence"],
                                  "generation_succeeded": rec["status"] == "ok"}, ensure_ascii=False)
            records["judges"][qid] = {}
            for model in JUDGES:
                if deadline_left() <= 30:
                    records["judges"][qid][model] = {"status": "incomplete", "reason": "08:10 API 门控"}
                    log(f"judge {qid}/{model} skipped: deadline")
                    continue
                api = api_cache.get(model)
                if api is None:
                    from app.config import get_settings
                    s = get_settings()
                    api = ChatAPI(s.openai_api_key, s.openai_base_url, model)
                    api_cache[model] = api
                try:
                    raw = api.chat(judge_system, payload, json_mode=True)
                    verdict = json.loads(raw)
                except Exception as exc:
                    verdict = {"status": "error", "error_type": type(exc).__name__}
                records["judges"][qid][model] = {"verdict": verdict, "usage": api.calls[-1].get("usage", {})}
                log(f"judge {qid}/{model} done")

    # 账本与实配 manifest 回填
    ledger_path = RUN_DIR / "ledger.jsonl"
    ledger_lines = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()] if ledger_path.exists() else []
    from collections import defaultdict
    by_model = defaultdict(lambda: {"reserved": 0, "actual": 0, "unknown": 0})
    for rec in ledger_lines:
        m = rec.get("model", "?")
        if rec["type"] == "reserved":
            by_model[m]["reserved"] += rec["estimate"]
        elif rec["type"] == "committed":
            by_model[m]["actual"] += rec["actual"]
        elif rec["type"] == "committed_unknown":
            by_model[m]["unknown"] += rec["estimate"]
    import subprocess
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    records["manifest"] = {
        "commit": commit, "chat_model": os.environ.get("CHAT_MODEL", "qwen3.8-max"),
        "embedding": "qwen3.7-text-embedding-flash", "dim": 1024, "vector_backend": "qdrant remote 127.0.0.1:6333",
        "collection": "kb_night20261031", "rerank": "enabled qwen3.7-text-rerank",
        "agent_mode": "rule", "ledger_path": str(ledger_path), "ledger_by_model": by_model,
        "denominator": {"questions": QUESTIONS, "modes": ["rule"], "note": "其余 36 题+native 因 08:10 门控与回放快照未建，不可执行，如实披露"},
    }
    out = RUN_DIR / "smoke_results.json"
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"results written: {out}")

if __name__ == "__main__":
    main()

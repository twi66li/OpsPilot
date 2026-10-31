"""T23 BGL 真实数据批次：真实日志行（来源行号留痕、标签列剥离）+ 真实链路 RAG。"""
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
RUN_DIR = Path(__file__).parent / "bgl_run"
RUN_DIR.mkdir(exist_ok=True)
BGL_ZIP = r"C:/Users/86187/Documents/Codex/2026-10-30/codex-4-codex-reviewer-gpt-6/work/public-data-source-cache/BGL.zip"

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

API_DEADLINE = datetime.now(timezone(timedelta(hours=8))).replace(hour=8, minute=10, second=0, microsecond=0)
LINE_START, LINE_END = 14700, 14760


def log(m):
    print(f"[{datetime.now(timezone(timedelta(hours=8))).strftime('%H:%M:%S')}] {m}", flush=True)


def left():
    return (API_DEADLINE - datetime.now(timezone(timedelta(hours=8)))).total_seconds()


def main():
    # 1) 提取真实日志行并构建语料（剥离首列标签；行号前缀可追溯）
    z = zipfile.ZipFile(BGL_ZIP)
    raw_lines = []
    with z.open("BGL.log") as f:
        for i, raw in enumerate(f, 1):
            if i > LINE_END:
                break
            if i >= LINE_START:
                raw_lines.append((i, raw.decode("utf-8", errors="replace").rstrip("\n")))
    alert_lines = [(i, ln) for i, ln in raw_lines if not ln.startswith("- ")]
    corpus = "\n".join(f"L{i}: " + ln.split(" ", 1)[1] for i, ln in raw_lines)
    (RUN_DIR / "bgl_corpus_L14700_L14760.txt").write_text(corpus, encoding="utf-8")
    import hashlib
    provenance = {"source_file": "BGL.zip/BGL.log",
                  "zip_sha256": hashlib.sha256(open(BGL_ZIP, "rb").read()).hexdigest(),
                  "corpus_lines": [LINE_START, LINE_END],
                  "label_rule": "首列标签（'-'或告警名）仅评分侧保留；正文剥离并加 L{行号} 前缀",
                  "alert_lines_in_block": [{"line": i, "label": ln.split(" ", 1)[0]} for i, ln in alert_lines]}
    (RUN_DIR / "bgl_provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"corpus lines {LINE_START}-{LINE_END}, alerts in block: {len(alert_lines)} at {[i for i, _ in alert_lines]}")

    scenarios = [
        {"id": "bgl-001",
         "question": "知识库的 BGL 日志摘录中，L14738 到 L14742 这些行记录了什么级别与内容的消息？请引用行号与时间戳。",
         "points": ["这些行为 RAS APP FATAL 级别的 ciod: fail 消息（ciod: failed to read object module 类）",
                    "时间戳为 1117869872/1117869873 即 2005.06.04 00.24.32-33", "引用了具体行号"]},
        {"id": "bgl-002",
         "question": "摘录范围内 INFO 级 KERNEL 消息主要涉及什么内容？出现最多的是哪条消息？",
         "points": ["主要是 instruction cache parity error corrected（指令缓存奇偶校验错误已纠正）",
                    "该消息在摘录中重复出现次数最多", "注明这些为已纠正（corrected）事件而非 FATAL"]},
        {"id": "bgl-003",
         "question": "摘录范围内是否存在 FATAL 级别日志？如有请给出行号，没有请明确说明。",
         "points": ["存在 FATAL 级别日志（RAS APP FATAL ciod）", "给出 L14738-L14742 行号",
                    "与 INFO 级 corrected 事件明确区分"]},
    ]

    from fastapi.testclient import TestClient

    from app.main import app
    records = {"namespace": "night20261031", "batch": "bgl_real", "questions": {}, "judges": {}, "manifest": {}}
    with TestClient(app) as client:
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
        sid = client.post("/api/sessions", json={"title": "night20261031-bgl"}, headers=headers).json()["id"]

        # DashScope embedding 单批 ≤20 条文本：按行拆为 4 篇小文档入库（行号前缀保留）
        bounds = [(14700, 14714), (14715, 14729), (14730, 14744), (14745, 14760)]
        for a, b in bounds:
            part = "\n".join(f"L{i}: " + ln.split(" ", 1)[1] for i, ln in raw_lines if a <= i <= b)
            r = client.post("/api/kb/documents",
                            json={"title": f"BGL 真实日志摘录 L{a}-L{b}", "content": part,
                                  "source": "BGL.zip/BGL.log"}, headers=headers)
            assert r.status_code == 200, r.text
        log("BGL corpus docs ingested (real embedding, 4 parts)")

        for case in scenarios:
            if left() <= 60:
                records["questions"][case["id"]] = {"status": "incomplete", "reason": "08:10 门控"}
                continue
            t0 = time.time()
            r = client.post("/api/agent/chat", json={"session_id": sid, "message": case["question"]}, headers=headers)
            body = r.json()
            events = body.get("events", [])
            hits = [h for e in events if e.get("type") == "retrieval" for h in e.get("hits", [])]
            rec = {"status": "ok" if body.get("status") in ("answered", "approval_required") else body.get("status"),
                   "question": case["question"], "points": case["points"], "answer": body.get("answer", ""),
                   "end_reason": body.get("end_reason"), "citations": body.get("citations", []),
                   "evidence": [{"doc_title": h.get("doc_title"), "text": (h.get("text") or "")[:400]} for h in hits[:5]],
                   "elapsed_s": round(time.time() - t0, 1)}
            records["questions"][case["id"]] = rec
            log(f"{case['id']} done {rec['status']} {rec['elapsed_s']}s")

        rubric = json.load(open(ROOT / "eval/night20261031/rubric.json", encoding="utf-8"))
        judge_system = ("你是评测裁判。仅依据题目、金标要点、rubric 与该答案实际取得的证据判分，"
                        "只输出 JSON：{\"answer_correct\":bool,\"grounded\":bool,\"appropriate_abstention\":bool,\"reason\":\"简述\"}。")
        from app.services.model_api import ChatAPI
        api_cache = {}
        for case in scenarios:
            rec = records["questions"].get(case["id"])
            if not rec or rec.get("status") == "incomplete":
                continue
            payload = json.dumps({"question": rec["question"], "required_points": rec["points"],
                                  "rubric": {k: rubric[k] for k in ("answer_correct", "grounded", "appropriate_abstention")},
                                  "answer": rec["answer"], "evidence": rec["evidence"],
                                  "generation_succeeded": rec["status"] == "ok"}, ensure_ascii=False)
            records["judges"][case["id"]] = {}
            for model in ["qwen3.8-max", "qwen3.8-27b"]:
                if left() <= 30:
                    records["judges"][case["id"]][model] = {"status": "incomplete", "reason": "08:10 门控"}
                    continue
                api = api_cache.get(model)
                if api is None:
                    from app.config import get_settings
                    s = get_settings()
                    api = ChatAPI(s.openai_api_key, s.openai_base_url, model)
                    api_cache[model] = api
                try:
                    verdict = json.loads(api.chat(judge_system, payload, json_mode=True))
                except Exception as exc:
                    verdict = {"status": "error", "error_type": type(exc).__name__}
                records["judges"][case["id"]][model] = {"verdict": verdict, "usage": api.calls[-1].get("usage", {})}
                log(f"judge {case['id']}/{model} done")

    from collections import defaultdict
    ledger_path = RUN_DIR / "ledger.jsonl"
    lines = [json.loads(x) for x in ledger_path.read_text(encoding="utf-8").splitlines() if x.strip()] if ledger_path.exists() else []
    by_model = defaultdict(lambda: {"reserved": 0, "actual": 0, "unknown": 0})
    for rec in lines:
        m = rec.get("model", "?")
        if rec["type"] == "reserved":
            by_model[m]["reserved"] += rec["estimate"]
        elif rec["type"] == "committed":
            by_model[m]["actual"] += rec["actual"]
        elif rec["type"] == "committed_unknown":
            by_model[m]["unknown"] += rec["estimate"]
    import subprocess
    records["manifest"] = {
        "commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip(),
        "data": provenance, "models": {"generation": "qwen3.8-max", "judges": ["qwen3.8-max", "qwen3.8-27b"]},
        "retrieval": {"collection": "kb_night20261031", "embedding": "qwen3.7-text-embedding-flash", "dim": 1024,
                      "rerank": "qwen3.7-text-rerank"},
        "task_scope_limit": "BGL 为日志 RAG 任务；native 工具循环与指标/历史工具不适用，未覆盖并如实限定",
        "denominator": {"questions": [c["id"] for c in scenarios], "modes": ["rule"],
                        "note": "night20261031 原 40 场景与 native 模式不可执行（时间门控/数据口径变更），如实披露"},
        "ledger_path": str(ledger_path), "ledger_by_model": by_model}
    (RUN_DIR / "bgl_results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"results written: {RUN_DIR / 'bgl_results.json'}")


if __name__ == "__main__":
    main()

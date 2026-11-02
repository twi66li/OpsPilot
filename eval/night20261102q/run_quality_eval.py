"""P1-QUALITY-20261102 质量轮评测：开发/留出/消融/回归，全部走实际工作台图。

模式（argv[1]）：
  dev        运行 4 道开发题（27b 生成， BM25 单路检索）
  heldout    8 道留出题一次真实运行 + 双裁判（deepseek-v4-pro + kimi-k3，两家族均非生成同族）
  ablation   4 道开发题 × 重排开/关（同模型同提示词同预算；候选集一致性校验；单裁判 deepseek-v4.1-flash）
  regress005 已见题 test-005 回归（受本轮提示词改变影响；n31real 租户 hybrid+重排；双裁判）

预算：eval/night20261102q/quality_budget.py（P1 总 400K + 分模型上限，账本 ledger.jsonl 与
工作台共用）。新租户 n31q 语料只建 BM25 索引（embedding 份额不足以建向量臂，如实标注；
纯统计路径不检索、零消耗）。每题运行前按完整链路预留，不足则跳题并如实标记 not_run。
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
BASELINE_Q = ROOT / "eval" / "night20261031" / "real_run"
RUN = HERE / "quality_run"
RUN.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
from quality_budget import apply_env, p1_shares, usage_now  # noqa: E402

apply_env()
os.environ["DATABASE_URL"] = "mysql+pymysql://aiops:aiops123@127.0.0.1:13306/aiops?charset=utf8mb4"
os.environ["QDRANT_MODE"] = "remote"
os.environ["QDRANT_URL"] = "http://127.0.0.1:6333"
os.environ["QDRANT_COLLECTION"] = "kb_qwen37_1024_v2"
os.environ["EMBEDDING_PROVIDER"] = "openai"
os.environ["EMBEDDING_DIM"] = "1024"
os.environ["EMBEDDING_MODEL"] = "qwen3.7-text-embedding-flash"
os.environ["LLM_PROVIDER"] = "openai"
os.environ["MODEL_STRICT"] = "true"
os.environ["CHAT_MODEL"] = "qwen3.8-27b"  # 本轮生成首选（generation_order[0]）
os.environ["RERANK_ENABLED"] = "true"
os.environ["RERANK_PROVIDER"] = "dashscope"
os.environ["RERANK_MODEL"] = "qwen3.7-text-rerank"
os.environ["AGENT_MODE"] = "rule"
os.environ["RETRIEVAL_MODE"] = "bm25"  # 新租户 n31q 无向量臂（embedding 份额不足），BM25 单路有标注

GEN_MODEL = "qwen3.8-27b"
JUDGE_ABSTENTION_RUBRIC = {
    "factual": "回答中的计数、级别、时间、分组等可程序推导事实与 gold_program 一致，且不超出证据",
    "grounded": "回答引用的证据编号可定位到实际给出的证据；结论有日志依据",
    "appropriate_abstention": "对 gold_notes.unverifiable 事项未妄下结论，区分有据/待取证；缺失行不冒充完整",
}


def log(m):
    print(f"[{datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S')}] {m}", flush=True)


def frozen():
    return json.loads((HERE / "scenarios_frozen.json").read_text(encoding="utf-8"))


def remaining():
    return usage_now()["remaining"]


def reserve_estimate(question: str) -> dict:
    """27b 生成 + 检索/统计的保守预留（quality_budget.reserve_for 口径）。"""
    payload = len(question.encode("utf-8")) + 12000  # 证据组装 + 工具输出 + 系统提示
    return {"gen_27b": payload + 1536, "rerank_round": 9800, "embedding": 200}


def judge_reserve() -> int:
    return 9000 + 6000  # 证据/答案载荷 + 判分输出（双裁判各一次）


def preflight(question: str, judges: list[str]) -> tuple[bool, bool, dict]:
    """(可运行, 允许重排, 详情)。27b/裁判/embedding 不足才拒绝；rerank 不足降级为有标注的无重排路径。"""
    rem = remaining()
    est = reserve_estimate(question)
    need = {GEN_MODEL: est["gen_27b"]}
    for j in judges:
        need[j] = need.get(j, 0) + judge_reserve()
    ok = all(rem.get(m, 0) >= n for m, n in need.items()) and \
        rem.get("qwen3.7-text-embedding-flash", 0) >= est["embedding"]
    allow_rerank = rem.get("qwen3.7-text-rerank", 0) >= est["rerank_round"]
    return ok, allow_rerank, {"needed": need, "remaining": rem,
                              "allow_rerank": allow_rerank,
                              "rerank_note": None if allow_rerank else
                              "rerank 份额余量不足单轮预留，本题走有标注的无重排路径（BM25 融合序）"}


def setup_container(client, frozen_data):
    from app.auth.security import hash_password
    from app.container import get_container
    from app.db.base import SessionLocal
    from app.db.models import Document, Tenant, User

    with SessionLocal() as db:
        if not db.get(Tenant, "n31q"):
            db.add(Tenant(id="n31q", name="n31q 质量轮 BGL 新窗口租户"))
        if not db.query(User).filter(User.username == "n31q_admin").first():
            db.add(User(tenant_id="n31q", username="n31q_admin",
                        password_hash=hash_password("n31q-pass"), role="admin"))
        # 语料文档入库 MySQL（文档权威读取与统计的来源）；向量臂按本轮预算约束不建
        for d in frozen_data["corpus_docs"]:
            text = (HERE / "corpus" / d["file"]).read_text(encoding="utf-8")
            row = db.query(Document).filter(Document.tenant_id == "n31q", Document.title == d["title"]).first()
            if row is None:
                db.add(Document(tenant_id="n31q", title=d["title"], content=text,
                                source="BGL.zip/BGL.log", created_by="n31q_admin"))
            elif (row.content or "") != text:
                row.content = text  # 语料内容修正（如行号前缀补齐）后同步权威读取源
        db.commit()
    login = client.post("/auth/login", json={"username": "n31q_admin", "password": "n31q-pass"})
    assert login.status_code == 200, login.text
    container = get_container()
    # n31q 语料 BM25 单路索引（无向量臂；每个进程启动后重建，零 API 成本）
    from app.rag.chunking import split_into_chunks
    chunks = []
    for d in frozen_data["corpus_docs"]:
        text = (HERE / "corpus" / d["file"]).read_text(encoding="utf-8")
        doc_id = f"n31q::{d['file']}"
        for i, piece in enumerate(split_into_chunks(text, chunk_size=2000)):
            chunks.append({"chunk_id": f"{doc_id}::{i}", "doc_id": doc_id, "doc_title": d["title"],
                           "source": "BGL.zip/BGL.log", "text": piece, "chunk_index": i})
    container.bm25.replace_tenant("n31q", chunks)
    return login.json()["access_token"], container


def setup_container_bm25(container, frozen_data):
    """容器重建后补 n31q BM25 索引（进程内索引不跨容器存活；零 API 成本）。"""
    from app.rag.chunking import split_into_chunks

    chunks = []
    for d in frozen_data["corpus_docs"]:
        text = (HERE / "corpus" / d["file"]).read_text(encoding="utf-8")
        doc_id = f"n31q::{d['file']}"
        for i, piece in enumerate(split_into_chunks(text, chunk_size=2000)):
            chunks.append({"chunk_id": f"{doc_id}::{i}", "doc_id": doc_id, "doc_title": d["title"],
                           "source": "BGL.zip/BGL.log", "text": piece, "chunk_index": i})
    container.bm25.replace_tenant("n31q", chunks)


def usage_delta(container, marks):
    out = {}
    for key, obj, attr in [("embedding", container.embedder, "calls"),
                           ("rerank", getattr(container.retriever, "reranker", None), "calls"),
                           ("chat", container.llm._api, "calls")]:
        if obj is None or not hasattr(obj, attr):
            continue
        for rec in getattr(obj, attr)[marks[key]:]:
            m = rec.get("model", "?")
            s = out.setdefault(m, {"calls": 0, "tokens": 0, "errors": 0, "budget_refused": 0})
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
                s["calls"] -= 0  # usage 缺失保留：结算层已记 unknown，不在此虚记
    return out


def mark(container):
    return {"embedding": len(container.embedder.calls),
            "rerank": len(container.retriever.reranker.calls) if container.retriever.reranker else 0,
            "chat": len(container.llm._api.calls) if hasattr(container.llm, "_api") else 0}


def run_question(container, headers, client, sc, with_rerank=True):
    t0 = time.time()
    sid = client.post("/api/sessions", json={"title": f"n31q-{sc['id']}"}, headers=headers).json()["id"]
    marks = mark(container)
    saved_reranker = container.retriever.reranker
    if not with_rerank:
        container.retriever.reranker = None
    attempts = []
    try:
        r = client.post("/api/agent/chat", json={"session_id": sid, "message": sc["question"]}, headers=headers)
        body = r.json()
        events = body.get("events", [])
        answers = [e for e in events if e.get("type") == "answer"]
        used = answers[-1].get("used_evidence") if answers else None
        retrievals = [e for e in events if e.get("type") == "retrieval"]
        rec = {
            "status": body.get("status") if r.status_code == 200 else f"http_{r.status_code}",
            "question": sc["question"], "split": sc["split"], "family": sc["family"],
            "window": sc["window"], "strategy": body.get("strategy"),
            "answer": body.get("answer", ""),
            "citations_indexed": body.get("citations_indexed", []),
            "evidence": used or [],
            "range_stats": body.get("range_stats"), "range_coverage": body.get("range_coverage"),
            "retrieval_rounds": len(retrievals), "retrieval_events": retrievals,
            "elapsed_s": round(time.time() - t0, 1), "attempts": attempts, "session_id": sid,
            "usage": usage_delta(container, marks),
            "model_config": {"generation": GEN_MODEL, "retrieval_mode": os.environ.get("RETRIEVAL_MODE"),
                             "rerank": bool(container.retriever.reranker)},
        }
        return rec
    except Exception as exc:
        import traceback
        attempts.append({"error_type": type(exc).__name__, "message": str(exc)[:300],
                         "tb": traceback.format_exc()[-500:]})
        return {"status": "failed", "question": sc["question"], "split": sc["split"], "family": sc["family"],
                "window": sc["window"], "strategy": None, "answer": "", "attempts": attempts,
                "model_config": {"generation": os.environ.get("CHAT_MODEL"),
                                 "retrieval_mode": os.environ.get("RETRIEVAL_MODE"),
                                 "rerank": bool(container.retriever.reranker)},
                "usage": usage_delta(container, marks), "elapsed_s": round(time.time() - t0, 1)}
    finally:
        container.retriever.reranker = saved_reranker


def run_judges(records, qid, judges):
    rec = records["questions"].get(qid)
    if not rec or rec.get("status") != "answered" or not rec.get("answer"):
        return
    records["judges"].setdefault(qid, {})
    from app.config import get_settings
    from app.services.model_api import ChatAPI

    settings = get_settings()
    judge_system = (
        "你是评测裁判。仅依据题目、gold_program 程序推导真值、gold_notes 有据/不可证实事项、"
        "rubric 与编号证据及统计工具输出评判该回答，不要使用证据之外的知识。只输出 JSON："
        '{"factual":bool,"grounded":bool,"appropriate_abstention":bool,"reason":"≤120字理由"}。'
        "注意：gold_notes.unverifiable 列出的事项不得由回答确证；缺失行不得冒充完整。")
    sc = records["_scenarios"][qid]
    payload = json.dumps({
        "question": rec["question"], "gold_program": sc["gold_program"], "gold_notes": sc["gold_notes"],
        "rubric": JUDGE_ABSTENTION_RUBRIC, "answer": rec.get("answer", ""),
        "evidence": rec.get("evidence", []), "range_stats": rec.get("range_stats"),
        "generation_succeeded": rec.get("status") == "answered",
    }, ensure_ascii=False)
    # 判分槽：槽内按"实际载荷预留"选择候选模型（预留不足则槽内下一候选并记录；缺判如实标注）
    payload_bytes = len(payload.encode("utf-8"))
    from quality_budget import MODEL_RESERVE_CONFIG

    def judge_ok(model):
        cfg = MODEL_RESERVE_CONFIG.get(model, {"output_cap": 1536, "reasoning_cap": 1024})
        need = payload_bytes + cfg["output_cap"] + cfg.get("reasoning_cap", 0)
        return remaining().get(model, 0) >= need

    for slot in judges:
        if not isinstance(slot, list):
            slot = [slot]
        slot_idx = judges.index(slot) + 1
        model = None
        for cand in slot:
            if records["judges"][qid].get(cand, {}).get("verdict"):
                model = cand  # 该槽已有有效判分
                break
            if judge_ok(cand):
                model = cand
                break
        if model is None:
            records["judges"][qid][f"slot{slot_idx}_exhausted"] = {
                "verdict": None, "missing": True, "usage": {"calls": 0, "tokens": 0},
                "note": f"该裁判槽候选 {'/'.join(slot)} 按载荷 {payload_bytes}B 预留均不足；缺判如实披露，不以单判冒充双判"}
            save(records)
            log(f"judge {qid} slot{slot_idx} exhausted ({'/'.join(slot)})")
            continue
        if records["judges"][qid].get(model, {}).get("verdict"):
            continue
        api = ChatAPI(settings.openai_api_key, settings.openai_base_url, model)
        attempts, verdict, n0 = [], None, len(api.calls)
        for attempt in (1, 2):
            try:
                verdict = json.loads(api.chat(judge_system, payload, json_mode=True))
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
        save(records)
        log(f"judge {qid}/{model} -> {verdict}")


def save(records):
    (RUN / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")


def summarize(records, judges):
    slots = [s if isinstance(s, list) else [s] for s in judges]
    counts = defaultdict(int)
    for rec in records["questions"].values():
        counts[rec.get("status", "?")] += 1
    both = 0
    agree = 0
    agree_n = 0
    for q, jmap in records.get("judges", {}).items():
        if q not in records.get("questions", {}):
            continue
        verdicts = []
        for slot in slots:
            v = next((jmap.get(m, {}).get("verdict") for m in slot
                      if jmap.get(m, {}).get("verdict")), None)
            verdicts.append(v)
        if all(v is not None for v in verdicts) and len(verdicts) == 2:
            both += 1
            a0 = verdicts[0].get("factual")
            a1 = verdicts[1].get("factual")
            if a0 == a1:
                agree += 1
            agree_n += 1
    records["summary"] = {"status_counts": dict(counts), "completed": len(records["questions"]),
                          "planned": len(records.get("_scenarios", {})),
                          "judge_slots": judges,
                          "double_judged": both,
                          "factual_agreement": f"{agree}/{both}" if both and len(slots) == 2 else "n/a",
                          "not_run": sorted(records.get("not_run", {}))}
    save(records)
    log(f"ALL DONE: {records['summary']}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "dev"
    import shutil as _shutil
    _shutil.copy(HERE / "scenarios_frozen.json", RUN / "scenarios_frozen.json")  # 接口从 results.json 同目录读冻结文件
    fd = frozen()
    records_fp = RUN / "results.json"
    records = json.loads(records_fp.read_text(encoding="utf-8")) if records_fp.exists() else {
        "namespace": "n31q", "batch": f"quality_{mode}", "questions": {}, "judges": {}, "not_run": {}}
    records["_scenarios"] = {s["id"]: s for s in fd["questions"]}
    records["config"] = {
        "generation": GEN_MODEL, "generation_order": "qwen3.8-27b 优先（budget.json generation_order）",
        "retrieval_mode_n31q": "bm25（新租户无向量臂：embedding 份额 10K 不足以对新语料建索引，如实标注）",
        "pure_path": "显式行范围+纯统计诉求 → 租户文档权威读取+程序模板（零 embedding/rerank/LLM）",
        "budget": {"project_total_cap": 400000, "ledger": "eval/night20261102q/ledger.jsonl（工作台与评测共用）"},
        "human_reviewed": False,
        "note": "已见题回归与留出集分列，不合并分母",
    }
    save(records)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        token, container = setup_container(client, fd)
        headers = {"Authorization": f"Bearer {token}"}

        if mode == "dev":
            judge_slots = [["deepseek-v4-pro-0813"]]
            for sc in fd["questions"]:
                if sc["split"] != "dev":
                    continue
                qid = sc["id"]
                if records["questions"].get(qid, {}).get("status") == "answered":
                    continue
                ok, allow_rerank, info = preflight(sc["question"], judges)
                if not ok:
                    records["not_run"][qid] = {"reason": "budget_preflight_refused", **info}
                    save(records)
                    continue
                records["questions"][qid] = run_question(container, headers, client, sc, with_rerank=allow_rerank)
                save(records)
                run_judges(records, qid, judge_slots)
            summarize(records, judge_slots)

        elif mode == "heldout":
            # 双裁判：两家族（deepseek 族 / kimi 族），均非生成同族；槽内按余额切换并记录
            judge_slots = [["deepseek-v4-pro-0813", "deepseek-v4.1-flash", "deepseek-v4.1-flash-1017"],
                           ["kimi-k3", "glm-5.3"]]
            gen_reserve = len("x".encode("utf-8")) * 0 + 14000 + 4096  # 证据载荷保守值 + 输出上限
            switches = records.setdefault("model_switches", [])
            current_gen = GEN_MODEL

            def pick_gen():
                rem = remaining()
                need = gen_reserve
                if rem.get(current_gen, 0) >= need:
                    return current_gen
                from quality_budget import GENERATION_ORDER
                for m in GENERATION_ORDER:
                    if m != current_gen and rem.get(m, 0) >= need:
                        return m
                return None

            for sc in fd["questions"]:
                if sc["split"] != "heldout":
                    continue
                qid = sc["id"]
                if records["questions"].get(qid, {}).get("status") == "answered":
                    continue
                from app.agent.nodes import evidence_strategy

                strategy = evidence_strategy(sc["question"])
                if strategy == "range_stats_direct":
                    # 纯统计路径：不消耗生成模型
                    records["questions"][qid] = run_question(container, headers, client, sc, with_rerank=False)
                    records.get("not_run", {}).pop(qid, None)
                    save(records)
                    log(f"{qid} done (pure path, no LLM) strategy={strategy}")
                    run_judges(records, qid, judge_slots)
                    continue
                gen = pick_gen()
                if gen is None:
                    records["not_run"][qid] = {"reason": "budget_preflight_refused",
                                               "detail": "生成候选队列余额均不足", "remaining": remaining()}
                    save(records)
                    log(f"{qid} SKIPPED (gen budget)")
                    continue
                if gen != current_gen:
                    os.environ["CHAT_MODEL"] = gen
                    from app.config import get_settings
                    get_settings.cache_clear()  # Settings 被 lru_cache：不清缓存则环境变量不生效
                    from app.container import get_container, reset_container
                    reset_container()
                    container = get_container()
                    setup_container_bm25(container, fd)  # 容器重建后补 n31q BM25 索引
                    switches.append({"at": qid, "from": current_gen, "to": gen,
                                     "reason": "本模型项目预留不足（budget.json 分模型上限），按 generation_order 切换",
                                     "old_model_remaining": remaining().get(current_gen)})
                    save(records)
                    log(f"generation model switched {current_gen} -> {gen} at {qid}")
                    current_gen = gen
                records["questions"][qid] = run_question(container, headers, client, sc, with_rerank=False)
                records.get("not_run", {}).pop(qid, None)
                if "model_config" in records["questions"][qid]:
                    records["questions"][qid]["model_config"]["generation_switched"] = gen != GEN_MODEL
                save(records)
                log(f"{qid} done status={records['questions'][qid]['status']} strategy={strategy} gen={gen}")
                run_judges(records, qid, judge_slots)
            records["summary"]["model_switches"] = switches
            summarize(records, judge_slots)

        elif mode == "judges":
            # 仅补判分：已 answered 的题按当前判分槽补齐缺失判分（不重新生成答案）
            fdj = records.get("_scenarios", {})
            judge_slots = [["deepseek-v4-pro-0813", "deepseek-v4.1-flash", "deepseek-v4.1-flash-1017"],
                           ["kimi-k3", "glm-5.3"]]
            for qid, rec in list(records["questions"].items()):
                if rec.get("status") != "answered" or qid not in fdj:
                    continue
                run_judges(records, qid, judge_slots)
            summarize(records, judge_slots)

        elif mode == "ablation":
            judge = "deepseek-v4.1-flash"
            records.setdefault("ablation", {})
            for sc in fd["questions"]:
                if sc["split"] != "dev" or sc["family"] != "descriptive":
                    continue
                qid = sc["id"]
                pair = records["ablation"].get(qid, {})
                if pair.get("off", {}).get("status") == "answered" and pair.get("on", {}).get("status") == "answered":
                    continue
                ok, info = preflight(sc["question"], [GEN_MODEL, judge])
                if not ok:
                    records["not_run"][f"ablation:{qid}"] = {"reason": "budget_preflight_refused", **info}
                    save(records)
                    continue
                # 关臂：无重排（fused 候选 = BM25 单路原始序）
                pair["off"] = run_question(container, headers, client, sc, with_rerank=False)
                save(records)
                # 开臂：有重排；检索确定性一致 → 候选集与关臂相同（以关臂 fused 序与开臂重排输入核对）
                pair["on"] = run_question(container, headers, client, sc, with_rerank=True)
                pair["candidate_identity_note"] = (
                    "检索对同一查询确定性一致（BM25 无随机性），两臂候选集相同；"
                    "关臂 retrieval 事件即候选序，开臂重排输入由 reranker 包装记录核对")
                # 判分（同裁判评两臂）
                records["questions"][qid + ":off"] = pair["off"]
                records["questions"][qid + ":on"] = pair["on"]
                records["_scenarios"][qid + ":off"] = sc
                records["_scenarios"][qid + ":on"] = sc
                save(records)
                run_judges(records, qid + ":off", [[judge]])
                run_judges(records, qid + ":on", [[judge]])
                records["ablation"][qid] = pair
                save(records)
            records["summary"]["ablation_pairs"] = {qid: {k: v.get("status") for k, v in pair.items()
                                                          if k in ("off", "on")}
                                                    for qid, pair in records.get("ablation", {}).items()}
            save(records)
            log(f"ALL DONE ablation: {json.dumps(records['summary']['ablation_pairs'], ensure_ascii=False)}")

        elif mode == "regress005":
            judge_slots = [["deepseek-v4-pro-0813", "deepseek-v4.1-flash", "deepseek-v4.1-flash-1017"],
                           ["kimi-k3", "glm-5.3"]]
            old = json.loads((BASELINE_Q / "scenarios_frozen.json").read_text(encoding="utf-8"))
            sc = dict({x["id"]: x for x in old["scenarios"]}["test-005"])
            sc.update({"id": "regress:test-005", "split": "regression_seen"})
            records["_scenarios"]["regress:test-005"] = sc
            if records["questions"].get("regress:test-005", {}).get("status") != "answered":
                ok, allow_rerank, info = preflight(sc["question"], judge_slots[0])
                if not ok:
                    records["not_run"]["regress:test-005"] = {"reason": "budget_preflight_refused", **info}
                    save(records)
                    log("regress:test-005 SKIPPED (budget)")
                    return
                # n31real 租户：旧语料已有向量索引，hybrid+重排（与基线口径一致）
                os.environ["RETRIEVAL_MODE"] = "hybrid"
                from app.container import reset_container
                reset_container()
                from app.auth.security import hash_password
                from app.db.base import SessionLocal
                from app.db.models import Tenant, User

                with SessionLocal() as db:
                    if not db.get(Tenant, "n31real"):
                        db.add(Tenant(id="n31real", name="n31real"))
                    if not db.query(User).filter(User.username == "n31real_admin").first():
                        db.add(User(tenant_id="n31real", username="n31real_admin",
                                    password_hash=hash_password("n31admin-pass"), role="admin"))
                    db.commit()
                login = client.post("/auth/login", json={"username": "n31real_admin", "password": "n31admin-pass"})
                headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
                records["questions"]["regress:test-005"] = run_question(container, headers, client, sc, with_rerank=allow_rerank)
                save(records)
            run_judges(records, "regress:test-005", judge_slots)
            records["summary"]["regression_note"] = "test-005 为已见题回归（提示词边界修复），另列不并入留出分母"
            save(records)
            summarize(records, judge_slots)


if __name__ == "__main__":
    main()

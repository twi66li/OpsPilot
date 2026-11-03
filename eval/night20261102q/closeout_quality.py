"""本次有限收尾：原答案复判、1题回归、4对控制变量重排；默认仅列计划。"""
import argparse
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "quality_closeout"
JUDGES = ["qwen3.8-27b", "deepseek-v4-pro-0813"]
FIELDS = ("factual", "grounded", "appropriate_abstention")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def main():
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--boundary-regression", nargs="?", const="v2", choices=["v2", "guard"])
    args = parser.parse_args()
    if args.boundary_regression:
        OUT = HERE / ("quality_boundary_guard" if args.boundary_regression == "guard" else "quality_boundary_fix")
    print("计划：只跑提示词边界修复后的test-005一次真实图回归与双判。" if args.boundary_regression else
          "计划：7份原留出答案原样复判 + q4更正题干纯统计回归；test-005真实图一次；4道开发题重排开关各一次。", flush=True)
    if not args.execute:
        return
    sys.path[:0] = [str(HERE), str(ROOT)]
    # 复用原运行的配置及记录函数，不调用其 main，不重跑旧批次。
    import run_quality_eval as original
    from quality_budget import usage_now
    os.environ["CHAT_MODEL_CANDIDATES"] = "[]"  # 对照与裁判固定模型，失败留痕，不换模型追分。
    from app.config import get_settings
    from app.container import get_container, reset_container
    from app.db.base import SessionLocal
    from app.db.models import User
    from app.services.model_api import ChatAPI
    from fastapi.testclient import TestClient
    from app.main import app

    OUT.mkdir(exist_ok=True)
    frozen = original.frozen()
    source = HERE / "quality_run/results.json"
    previous = json.loads(source.read_text(encoding="utf-8"))
    output = OUT / "results.json"
    records = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {
        "namespace": "n31q + n31real(test-005)", "batch": "quality_closeout_20261103",
        "gold_version": frozen["gold_version"], "questions": {}, "judges": {}, "ablation": {},
        "_scenarios": {}, "not_run": {}, "budget_before": usage_now(),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "config": {"generation": "原回答保留；q5/q7实际为qwen3.8-2.4t-a95b；新生成固定qwen3.8-27b",
                   "judges": JUDGES, "self_judge_disclosure": "27b与27b/2.4t生成同家族自评；DeepSeek为跨家族裁判",
                   "rerank": "仅4对开发集消融及test-005回归开启；旧留出原答案不改变",
                   "budget": "同一本轮P1权威账本与调整后总额，不重置起点；旧400K下失败保留",
                   "limitations": "原留出已暴露失败，不宣称新盲测；q4题干变化和test-005单列已见回归；小样本单次对照不证明普遍增益。",
                   "human_reviewed": False}}
    if args.boundary_regression:
        from app.agent.llm import diagnosis_system_prompt
        records["batch"] = "quality_boundary_fix_20261103"
        records["config"].update(generation="qwen3.8-27b", rerank="test-005真实hybrid回归开启",
            limitations="test-005为已见题；本次仅验证corrected/INFO不能推出运行状态与换件结论。旧提示词消融不重跑，仍单列保存。",
            previous_failed_run="night20261102q/quality_closeout",
            generation_prompt_sha256=hashlib.sha256(diagnosis_system_prompt().encode()).hexdigest())
        if args.boundary_regression == "guard":
            records["batch"] = "quality_boundary_guard_20261103"
            records["config"].update(generation="最终回答为程序证据边界模板；27b仅作相关性判断",
                previous_failed_run="night20261102q/quality_boundary_fix",
                limitations="已见题代码边界回归；两次模型回答及裁判漏判均保留，不以全绿替代原文回查。")
        (OUT / "generation_prompt_v2.json").write_text(json.dumps({"system": diagnosis_system_prompt()},
            ensure_ascii=False, indent=2), encoding="utf-8")

    def save():
        output.write_text(json.dumps(records, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        questions = list(records["_scenarios"].values())
        (OUT / "scenarios_frozen.json").write_text(json.dumps({"gold_version": frozen["gold_version"],
            "questions": questions}, ensure_ascii=False, indent=1), encoding="utf-8")

    def judge(qid, models=JUDGES):
        rec, sc = records["questions"][qid], records["_scenarios"][qid]
        payload = {"question": rec["question"], "answer": rec["answer"],
                   "gold_program": sc.get("gold_program", {}), "gold_notes": sc.get("gold_notes", {}),
                   "required_points": sc.get("required_points", []), "rubric": original.JUDGE_ABSTENTION_RUBRIC,
                   "evidence": rec["evidence"], "range_stats": rec.get("range_stats"),
                   "range_coverage": rec.get("range_coverage")}
        system = ('仅依据所给问题、gold_program、gold_notes、required_points、rubric、实际编号证据和统计判定回答。'
                  '不要使用外部知识。只输出JSON：{"factual":bool,"grounded":bool,'
                  '"appropriate_abstention":bool,"reason":"不超过120字"}。'
                  '无监控证据不能断言系统当前或历史整体正常。列出可能解释且明确待取证不等于确证根因。')
        for model in models:
            jmap = records["judges"].setdefault(qid, {})
            if model in jmap:
                continue  # 有效/失败均保留；显式失败不自动重试。
            api = ChatAPI(get_settings().openai_api_key, get_settings().openai_base_url, model)
            result = {"verdict": None, "missing": True, "answer_sha256": digest(rec["answer"]),
                      "payload_sha256": digest(payload), "gold_version": frozen["gold_version"]}
            try:
                raw = api.chat(system, json.dumps(payload, ensure_ascii=False), json_mode=True, output_cap=768)
                result["raw"] = raw
                verdict = json.loads(raw)
                assert all(isinstance(verdict.get(k), bool) for k in FIELDS)
                result.update(verdict=verdict, missing=False)
            except Exception as exc:
                result["error"] = {"type": type(exc).__name__, "message": str(exc)[:300]}
            result["usage"] = {"calls": len(api.calls), "tokens": sum(
                int((c.get("usage") or {}).get("total_tokens") or 0) for c in api.calls)}
            result["calls"] = api.calls
            jmap[model] = result
            with (OUT / "judge_trace.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"qid": qid, "model": model, "system": system,
                                    "payload": payload, "result": result}, ensure_ascii=False) + "\n")
            save()
            print(qid, model, result.get("verdict") or result.get("error"), flush=True)

    with TestClient(app) as client:
        if args.boundary_regression:
            container, headers = get_container(), {}
        else:
            token, container = original.setup_container(client, frozen)
            headers = {"Authorization": f"Bearer {token}"}
        for sc in frozen["questions"]:
            if args.boundary_regression or sc["split"] != "heldout":
                continue
            qid = sc["id"]
            records["_scenarios"][qid] = copy.deepcopy(sc)
            if qid not in records["questions"]:
                if qid == "heldout-q4":
                    rec = original.run_question(container, headers, client, sc, with_rerank=False)
                    assert rec["status"] == "answered" and not rec["usage"]
                    rec["split"] = "regression_seen_question_correction"
                    rec["origin"] = "题干明确冒号后的实际纯统计图回归；旧问题/旧答案留在quality_run"
                else:
                    rec = copy.deepcopy(previous["questions"][qid])
                    rec["origin"] = "原留出已存答案原样复用；只更正金标后复判，不重检索生成"
                    rec["source_answer_sha256"] = digest(rec["answer"])
                    rec["original_model_config"] = copy.deepcopy(rec.get("model_config"))
                    actual = [m for m in rec.get("usage", {}) if m not in (
                        "qwen3.7-text-rerank", "qwen3.7-text-embedding-flash")]
                    rec["model_config"]["generation"] = actual[-1] if actual else "程序模板（实际零LLM）"
                records["questions"][qid] = rec
                save()
            judge(qid)

        # 同一候选集、同一生成提示、同一模型、同一证据预算；只改变是否调用真实重排。
        # shortcut: 单次小样本且所有候选最终保留，只衡量排序，不推断大规模召回增益。
        from app.agent.nodes import DiagnosisNodes
        from app.services import chat_service
        nodes = DiagnosisNodes(container)
        for sc in frozen["questions"]:
            if args.boundary_regression or sc["split"] != "dev":
                continue
            pair = records["ablation"].setdefault(sc["id"], {})
            candidates = container.bm25.search("n31q", sc["question"], top_k=container.settings.top_k)
            frozen_candidates = [c.to_dict() for c in candidates]
            assert candidates
            pair["candidates"] = frozen_candidates
            pair["candidate_sha256"] = digest(frozen_candidates)
            for arm in ("off", "on"):
                qid = f"ablation:{sc['id']}:{arm}"
                records["_scenarios"][qid] = {**sc, "id": qid, "split": "dev_ablation"}
                if qid not in records["questions"]:
                    marks = original.mark(container)
                    hits = copy.deepcopy(candidates)
                    input_hash = digest([c.to_dict() for c in hits])
                    assert input_hash == pair["candidate_sha256"]
                    t0 = time.perf_counter()
                    if arm == "on":
                        hits = container.retriever.reranker.rerank(sc["question"], hits, top_k=container.settings.top_k)
                    with SessionLocal() as db:
                        user = db.query(User).filter(User.username == "n31q_admin").one()
                        session = chat_service.create_session(db, "n31q", user.id, qid)
                        sid = session.id
                    result = nodes.generate({"question": sc["question"], "tenant_id": "n31q", "session_id": sid,
                                             "hits": [h.to_dict() for h in hits], "tool_events": [], "fault_records": []})
                    rec = {"question": sc["question"], "split": "dev_ablation", "family": sc["family"],
                           "window": sc["window"], "status": "answered", "answer": result["answer"],
                           "evidence": result["used_evidence"], "range_stats": result.get("range_stats"),
                           "range_coverage": result.get("range_coverage"), "elapsed_s": round(time.perf_counter()-t0, 2),
                           "usage": original.usage_delta(container, marks), "candidate_sha256": input_hash,
                           "ranked_chunk_ids": [h.chunk_id for h in hits], "strategy": "controlled_single_pass",
                           "model_config": {"generation": original.GEN_MODEL, "retrieval_mode": "bm25", "rerank": arm == "on"},
                           "origin": "真实BM25候选冻结→可选真实重排→生产generate节点；不含grade/rewrite以隔离变量"}
                    records["questions"][qid] = rec
                    pair[arm] = {"qid": qid, "candidate_sha256": input_hash}
                    save()
                judge(qid, [JUDGES[1]])
            assert pair["off"]["candidate_sha256"] == pair["on"]["candidate_sha256"]

        # 单独的已见题真实图回归：沿用原租户和索引、一次生成，不与留出分母混合。
        old_freeze = json.loads((original.BASELINE_Q / "scenarios_frozen.json").read_text(encoding="utf-8"))
        sc = copy.deepcopy(next(s for s in old_freeze["scenarios"] if s["id"] == "test-005"))
        qid = "regress:test-005"
        sc.update(id=qid, split="regression_seen")
        raw = (original.BASELINE_Q / "corpus/bgl_testA_L500_L534.txt").read_text(encoding="utf-8").splitlines()
        assert len(raw) == 35 and all("instruction cache parity error corrected" in line for line in raw)
        sc["gold_program"] = {"corrected_instruction_cache_parity": 35, "source_range": [500, 534]}
        sc["gold_notes"] = {"unverifiable": ["系统当前或历史整体运行正常", "无服务中断", "硬件根因或需要换件"]}
        records["_scenarios"][qid] = sc
        if qid not in records["questions"]:
            os.environ["RETRIEVAL_MODE"] = "hybrid"
            get_settings.cache_clear()
            reset_container()
            container = get_container()
            from app.services.kb_service import warm_indexes
            with SessionLocal() as db:
                warm_indexes(db, container)
            login = client.post("/auth/login", json={"username": "n31real_admin", "password": "n31admin-pass"})
            assert login.status_code == 200
            records["questions"][qid] = original.run_question(container,
                {"Authorization": f"Bearer {login.json()['access_token']}"}, client, sc, with_rerank=True)
            save()
        judge(qid)

    def score(ids, models):
        valid = both = agree = all_good = 0
        missing, disagreements = [], []
        for qid in ids:
            vs = [records["judges"].get(qid, {}).get(m, {}).get("verdict") for m in models]
            if not all(v and all(isinstance(v.get(k), bool) for k in FIELDS) for v in vs):
                missing.append(qid)
                continue
            valid += 1
            both += all(v["factual"] for v in vs)
            all_good += all(all(v[k] for k in FIELDS) for v in vs)
            if len(vs) == 2:
                agree += vs[0]["factual"] == vs[1]["factual"]
                if vs[0]["factual"] != vs[1]["factual"]:
                    disagreements.append(qid)
        return {"planned": len(ids), "valid": valid, "all_factual_true": both, "all_dimensions_true": all_good,
                "factual_agree": agree if len(models) == 2 else None, "missing": missing, "disagreements": disagreements}

    reused = [f"heldout-q{i}" for i in (1, 2, 3, 5, 6, 7, 8)]
    records["summary"] = {"completed": len(records["questions"]), "planned": 17,
        "reused_heldout": score(reused, JUDGES), "corrected_question_seen": score(["heldout-q4"], JUDGES),
        "regression_seen": score(["regress:test-005"], JUDGES),
        "ablation": {arm: score([f"ablation:dev-q{i}:{arm}" for i in range(1, 5)], [JUDGES[1]]) for arm in ("off", "on")},
        "executed_ablation_pairs": len(records["ablation"]),
        "note": "不合并留出、题干修正回归、已见回归、开发消融分母；旧v1判分仍作废；本次Codex自修自验。"}
    records["budget_after"] = usage_now()
    if args.boundary_regression:
        result = score(["regress:test-005"], JUDGES)
        records["summary"] = {"completed": len(records["questions"]), "planned": 1,
            "status_counts": {records["questions"]["regress:test-005"]["status"]: 1},
            "double_judged": result["valid"], "factual_agreement": f"{result['factual_agree']}/{result['valid']}",
            "regression_seen": result, "note": "旧失败保留；代码证据边界已见题回归，不能视为新留出结果。" if args.boundary_regression == "guard"
            else "旧失败保留；提示词v2已见题回归，不能视为新留出结果。"}
    save()
    print(json.dumps(records["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

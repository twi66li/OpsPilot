"""按 gold_v2_20261101 更正复判受影响既有答案（终审 P1-GOLD 验收）。

只调裁判，不重新生成答案。受影响既有答案集合（由更正记录与既有批次确定）：
- test-004 基线答案（eval/night20261031/real_run）
- test-030 基线答案（同上；重排批分层样本不含 test-030，无既有答案需复判，
  其重排链路成绩将在 run_review_fix_verify.py 的扩展范围中重新生成并判分）
复判结果单独落盘（gold_corrections/refill_judgments.json），不改写旧成绩；
用量记入本轮账本 eval_ledger.jsonl。
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
BASELINE = ROOT / "eval" / "night20261031" / "real_run"
LEDGER = HERE / "eval_ledger.jsonl"
OUT = HERE / "gold_corrections" / "refill_judgments.json"

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
from p1_budget import effective_limits  # noqa: E402

MODEL_LIMITS, TOTAL_LIMIT = effective_limits(LEDGER, [HERE / "workbench" / "ledger.jsonl"])
os.environ["BUDGET_LEDGER_PATH"] = str(LEDGER)
os.environ["BUDGET_TOKEN_LIMIT"] = str(TOTAL_LIMIT)
os.environ["BUDGET_MODEL_LIMITS"] = json.dumps(MODEL_LIMITS)

JUDGES = ["qwen3.8-max-0902", "qwen3.8-27b"]
RUBRIC = {"answer_correct": "回答是否覆盖金标要点且无事实错误（含计数/行号/时间戳/节点）",
          "grounded": "回答是否仅依据给出的编号证据，无编造（引用可定位到证据编号）",
          "appropriate_abstention": "对无法从证据得知的问题（含 must_abstain 题）是否拒绝编造并说明依据不足"}
JUDGE_SYSTEM = (
    "你是评测裁判。仅依据题目、金标要点、rubric 与编号证据评判该回答，"
    "不要使用证据之外的知识。只输出 JSON："
    "{\"answer_correct\":bool,\"grounded\":bool,\"appropriate_abstention\":bool,\"reason\":\"≤120字理由\"}。"
    "注意：若金标要点本身要求“明确说明不存在/无法断定”，回答做到即算 appropriate_abstention=true。")


def load_out():
    if OUT.exists():
        return json.loads(OUT.read_text(encoding="utf-8"))
    return {"version": "gold_v2_20261101_refill", "records": {}}


def judge_once(api, payload_json):
    import time

    attempts, verdict, n0 = [], None, len(api.calls)
    for attempt in (1, 2):
        try:
            verdict = json.loads(api.chat(JUDGE_SYSTEM, payload_json, json_mode=True))
            break
        except Exception as exc:
            attempts.append({"attempt": attempt, "error_type": type(exc).__name__, "message": str(exc)[:200]})
    jcalls = api.calls[n0:]
    return verdict, attempts, {"calls": len(jcalls),
                               "tokens": sum(int((c.get("usage") or {}).get("total_tokens") or 0) for c in jcalls),
                               "unknown": sum(1 for c in jcalls if not (c.get("usage") or {}).get("total_tokens"))}


def main():
    from app.config import get_settings
    from app.services.model_api import ChatAPI

    corr = json.loads((HERE / "gold_corrections" / "corrections.json").read_text(encoding="utf-8"))
    cmap = {c["id"]: c for c in corr["corrections"]}
    base = json.loads((BASELINE / "results.json").read_text(encoding="utf-8"))
    settings = get_settings()
    out = load_out()

    targets = [
        ("test-004", "baseline", base, cmap["test-004"]),
        ("test-030", "baseline", base, cmap["test-030"]),
    ]
    for qid, run_name, records, c in targets:
        key = f"{run_name}:{qid}"
        rec = records["questions"][qid]
        if rec.get("status") == "failed" and not rec.get("answer"):
            continue
        if key in out["records"] and all(out["records"][key]["judges"].get(m, {}).get("verdict") for m in JUDGES):
            continue
        payload = json.dumps({
            "question": c["old"]["question"],  # 题目按生成时原样呈现
            "required_points": c["new"]["required_points"],  # 金标按更正版
            "must_abstain": rec.get("must_abstain", False),
            "rubric": RUBRIC,
            "gold_correction_note": f"金标已按原始日志更正（{corr['version']}）：{c['reason']}；评判以更正后要点为准，"
                                    "答案中与原始日志一致的时间戳/计数不因旧题干示例有误而判错",
            "answer": rec.get("answer", ""), "evidence": rec.get("evidence", []),
            "generation_succeeded": rec.get("status") == "answered",
        }, ensure_ascii=False)
        entry = {"run": run_name, "question_id": qid, "judges": {}, "regenerated": False}
        for model in JUDGES:
            api = ChatAPI(settings.openai_api_key, settings.openai_base_url, model)
            verdict, attempts, usage = judge_once(api, payload)
            entry["judges"][model] = {"verdict": verdict, "attempts": attempts, "usage": usage,
                                      "missing": verdict is None}
        out["records"][key] = entry
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"refilled {key}: " + json.dumps({m: (v['verdict'] or {}).get('answer_correct') for m, v in entry['judges'].items()}, ensure_ascii=False))

    out["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("DONE")


if __name__ == "__main__":
    main()

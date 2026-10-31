"""补齐 night20261031 基线批次缺判（run-20261101 授权后的第一笔评测调用）。

范围与约束：
- 只补裁判，不重新生成答案：答案与编号证据取 real_run/results.json 已落盘记录
  （即基线生成器实际收到的 [:500] 截断证据），判题载荷结构与基线脚本逐字一致；
- 主判 qwen3.8-max-0902 补 test-029/030、reg-001..003 共 5 题（此前因旧 0902 槽耗尽
  在到达供应商前被拒，无实际消耗）；
- 双裁判既有分歧（test-017/026/027）追加 deepseek-v4-pro-0813 跨家族复核，
  结果单独存 cross_reviews，不改动双裁判记录；
- 用量记入本轮新账本 eval_ledger.jsonl（p1 份额合并核算），不占用旧账本口径。
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
BASELINE = ROOT / "eval" / "night20261031" / "real_run"
LEDGER = HERE / "eval_ledger.jsonl"

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
from p1_budget import effective_limits, p1_shares  # noqa: E402

MODEL_LIMITS, TOTAL_LIMIT = effective_limits(LEDGER, [HERE / "workbench" / "ledger.jsonl"])
os.environ["BUDGET_LEDGER_PATH"] = str(LEDGER)
os.environ["BUDGET_TOKEN_LIMIT"] = str(TOTAL_LIMIT)
os.environ["BUDGET_MODEL_LIMITS"] = json.dumps(MODEL_LIMITS)

REFILL_QUESTIONS = ["test-029", "test-030", "reg-001", "reg-002", "reg-003"]
CROSS_REVIEW_QUESTIONS = ["test-017", "test-026", "test-027"]
MAIN_JUDGE = "qwen3.8-max-0902"
CROSS_JUDGE = "deepseek-v4-pro-0813"


def log(m):
    print(f"[{datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S')}] {m}", flush=True)


def judge_once(api, qid, payload_json):
    """单次裁判调用（最多额外重试 1 次），返回 (verdict, attempts, calls, tokens, unknown)。"""
    from app.config import get_settings
    from app.services.model_api import ChatAPI

    judge_system = (
        "你是评测裁判。仅依据题目、金标要点、rubric 与编号证据评判该回答，"
        "不要使用证据之外的知识。只输出 JSON："
        "{\"answer_correct\":bool,\"grounded\":bool,\"appropriate_abstention\":bool,\"reason\":\"≤120字理由\"}。"
        "注意：若金标要点本身要求“明确说明不存在/无法断定”，回答做到即算 appropriate_abstention=true。")
    attempts, n0 = [], len(api.calls)
    verdict = None
    for attempt in (1, 2):
        try:
            text = api.chat(judge_system, payload_json, json_mode=True)
            verdict = json.loads(text)
            break
        except Exception as exc:
            attempts.append({"attempt": attempt, "error_type": type(exc).__name__, "message": str(exc)[:200]})
            log(f"judge {qid} attempt {attempt} failed: {type(exc).__name__}")
    jcalls = api.calls[n0:]
    return verdict, attempts, jcalls, sum(
        int((c.get("usage") or {}).get("total_tokens") or 0) for c in jcalls), len(
        [c for c in jcalls if not (c.get("usage") or {}).get("total_tokens")])


def build_payload(rec):
    rubric = {"answer_correct": "回答是否覆盖金标要点且无事实错误（含计数/行号/时间戳/节点）",
              "grounded": "回答是否仅依据给出的编号证据，无编造（引用可定位到证据编号）",
              "appropriate_abstention": "对无法从证据得知的问题（含 must_abstain 题）是否拒绝编造并说明依据不足"}
    return json.dumps({
        "question": rec["question"], "required_points": rec["required_points"],
        "must_abstain": rec["must_abstain"], "rubric": rubric,
        "answer": rec.get("answer", ""), "evidence": rec.get("evidence", []),
        "generation_succeeded": rec.get("status") == "answered",
    }, ensure_ascii=False)


def main():
    from app.config import get_settings
    from app.services.model_api import ChatAPI

    settings = get_settings()
    records = json.loads((BASELINE / "results.json").read_text(encoding="utf-8"))
    apis = {}

    def get_api(model):
        if model not in apis:
            apis[model] = ChatAPI(settings.openai_api_key, settings.openai_base_url, model)
        return apis[model]

    # ---- 主判补缺：载荷与基线逐字同构（同 rubric、同答案、同编号证据） ----
    for qid in REFILL_QUESTIONS:
        rec = records["questions"].get(qid)
        cur = records.get("judges", {}).get(qid, {}).get(MAIN_JUDGE, {})
        if rec is None:
            log(f"skip {qid}: no record")
            continue
        if cur.get("verdict"):
            log(f"skip {qid}: main judge already present")
            continue
        verdict, attempts, jcalls, tokens, unknown = judge_once(get_api(MAIN_JUDGE), qid, build_payload(rec))
        entry = {
            "verdict": verdict, "attempts": attempts,
            "usage": {"calls": len(jcalls), "tokens": tokens, "unknown": unknown},
            "missing": verdict is None,
            "refill": True,
            "refill_note": "2026-11-01 run-20261101 授权后补判：答案/证据复用基线落盘记录，未重新生成",
        }
        if cur:
            entry["earlier_attempts"] = cur.get("attempts", [])  # 保留旧失败留痕（预算拒答，无消耗）
        records["judges"].setdefault(qid, {})[MAIN_JUDGE] = entry
        (BASELINE / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"refill {qid}/{MAIN_JUDGE} -> {verdict}")

    # ---- 跨家族复核：双裁判分歧题（deepseek 独立视角，单独存放） ----
    cross = records.setdefault("cross_reviews", {})
    for qid in CROSS_REVIEW_QUESTIONS:
        rec = records["questions"].get(qid)
        if rec is None or cross.get(qid, {}).get(CROSS_JUDGE, {}).get("verdict"):
            continue
        verdict, attempts, jcalls, tokens, unknown = judge_once(get_api(CROSS_JUDGE), qid, build_payload(rec))
        cross[qid] = {CROSS_JUDGE: {
            "verdict": verdict, "attempts": attempts,
            "usage": {"calls": len(jcalls), "tokens": tokens, "unknown": unknown},
            "missing": verdict is None,
            "role": "cross_family_disagreement_review",
            "note": "对 0902 与 27b 判分分歧题的跨家族复核；不覆盖双裁判记录，仅作第三视角披露",
        }}
        (BASELINE / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"cross-review {qid}/{CROSS_JUDGE} -> {verdict}")

    # ---- 汇总刷新（与基线脚本同口径）+ 披露追加 ----
    from collections import defaultdict
    JUDGES = ["qwen3.8-max-0902", "qwen3.8-27b"]
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
                          "target": 43, "judged_main": judged, "both_judged": both,
                          "judge_agreement_on_answer_correct": f"{agree}/{both}" if both else "0/0"}
    records["manifest"]["refill"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "refilled_main_judge": [q for q in REFILL_QUESTIONS
                                if records["judges"].get(q, {}).get(MAIN_JUDGE, {}).get("verdict")],
        "cross_reviews": {q: bool(v.get(CROSS_JUDGE, {}).get("verdict")) for q, v in cross.items()},
        "ledger": str(LEDGER),
        "note": "补判与复核用量记入本轮新账本（budget.json p1 份额），不占用旧 real_run/ledger.jsonl 口径",
    }
    disclosure = ("缺判补齐：2026-11-01 新授权后仅调用裁判补判 5 题（答案与证据复用基线落盘记录，未重新生成），"
                  "并对 3 题双裁判分歧追加 DeepSeek 跨家族复核（cross_reviews 字段，第三视角，不改双裁判结论）")
    if disclosure not in records["manifest"]["disclosures"]:
        records["manifest"]["disclosures"].append(disclosure)
    (BASELINE / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"DONE summary={records['summary']}")


if __name__ == "__main__":
    main()

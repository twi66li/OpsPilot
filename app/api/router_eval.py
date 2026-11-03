"""评测结果接口：admin 只读读盘，支持多运行（历史基线 / 重排恢复批等）。

发现规则：扫描 eval/night*/*/results.json（评测脚本落盘目录），不提供写接口，
不做路径拼接（run 参数必须命中发现清单，防目录穿越）。
"""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.deps import AuthUser, require_roles

router = APIRouter(prefix="/api/eval", tags=["eval-results"])

_EVAL_ROOT = Path(__file__).resolve().parents[2] / "eval"


def _discover_runs() -> dict[str, dict]:
    runs: dict[str, dict] = {}
    for results_fp in sorted(_EVAL_ROOT.glob("night*/*/results.json")):
        run_id = results_fp.parent.relative_to(_EVAL_ROOT).as_posix()
        try:
            records = json.loads(results_fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cfg = records.get("config", {})
        rerank_desc = str(cfg.get("rerank", ""))
        runs[run_id] = {
            "id": run_id,
            "namespace": records.get("namespace"),
            "batch": records.get("batch"),
            "rerank_active": bool(rerank_desc) and "停用" not in rerank_desc,
            "rerank_desc": rerank_desc,
            "completed": (records.get("summary") or {}).get("completed"),
            "target": (records.get("summary") or {}).get("target") or (records.get("summary") or {}).get("planned"),
            "judge_agreement": (records.get("summary") or {}).get("judge_agreement_on_answer_correct"),
        }
    return runs


def _load_run(run_id: str) -> tuple[dict, dict]:
    runs = _discover_runs()
    if run_id and run_id not in runs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"评测运行不存在：{run_id}")
    run_id = run_id or next(iter(runs))
    fp = _EVAL_ROOT / run_id / "results.json"
    records = json.loads(fp.read_text(encoding="utf-8"))
    frozen_fp = fp.parent / "scenarios_frozen.json"
    frozen = json.loads(frozen_fp.read_text(encoding="utf-8")) if frozen_fp.exists() else {}
    return records, {"frozen": frozen, "meta": runs[run_id]}


@router.get("/runs")
def eval_runs(user: AuthUser = Depends(require_roles("admin"))):
    """列出可查看的评测运行（历史基线 / 重排恢复批），供工作台切换。"""
    return {"runs": list(_discover_runs().values())}


@router.get("/results")
def eval_results(user: AuthUser = Depends(require_roles("admin")), run: str = ""):
    """返回指定运行的结果：总体完成数、逐题输入/编号证据/答案/双裁判判分/失败/用量。

    数据来源为评测脚本落盘的 results.json 与 scenarios_frozen.json（只读）。缺省返回
    发现清单中的第一个运行（night20261031/real_run 历史基线）。
    """
    records, ctx = _load_run(run)
    # 冻结文件兼容：历史批为 scenarios[]，quality 轮为 questions[]（含 gold_program/gold_notes）
    frozen_qs = ctx["frozen"].get("scenarios") or ctx["frozen"].get("questions") or []
    by_id = {s["id"]: s for s in frozen_qs}
    questions = []
    for qid in sorted(records.get("questions", {})):
        rec = records["questions"][qid]
        s = by_id.get(qid, {})
        judges = records.get("judges", {}).get(qid, {})
        questions.append({
            "id": qid,
            "split": rec.get("split") or s.get("split"),
            "family": rec.get("family") or s.get("family"),
            "window": rec.get("window") or s.get("window"),
            "status": rec.get("status"),
            "question": rec.get("question"),
            "must_abstain": rec.get("must_abstain", False),
            "rerank_active": rec.get("rerank_active", (rec.get("model_config") or {}).get("rerank")),
            "strategy": rec.get("strategy"),
            "range_stats": rec.get("range_stats"),
            "range_coverage": rec.get("range_coverage"),
            "model_config": rec.get("model_config"),
            "required_points": rec.get("required_points") or s.get("required_points", []),
            "gold_program": rec.get("gold_program") or s.get("gold_program"),
            "gold_notes": rec.get("gold_notes") or s.get("gold_notes"),
            "answer": rec.get("answer", ""),
            "citations": rec.get("citations", []),
            "evidence": rec.get("evidence", []),
            # 判分 schema 兼容（P1-Q-REPORT）：quality 轮裁判为 factual 口径，映射到旧页面
            # 读取的 answer_correct；原始 verdict 原样保留，缺判如实透出
            "judges": {m: {"verdict": v.get("verdict"), "missing": v.get("missing", True),
                           "answer_correct": (v.get("verdict") or {}).get("answer_correct",
                                            (v.get("verdict") or {}).get("factual")),
                           "usage": v.get("usage", {}), "note": v.get("note")} for m, v in judges.items()},
            "usage": rec.get("usage", {}),
            "error": rec.get("error") or (rec.get("attempts") or None),
            "elapsed_s": rec.get("elapsed_s"),
        })
    return {
        "id": ctx["meta"]["id"],
        "namespace": records.get("namespace"),
        "batch": records.get("batch"),
        "gold_version": records.get("gold_version"),
        "judging_invalidated": records.get("summary", {}).get("judging_invalidated"),
        "config": records.get("config", {}),
        "summary": records.get("summary", {}),
        "manual_audit": records.get("manual_audit", {}),
        "manifest": records.get("manifest") or {"ledger_by_model": records.get("budget_after", {}).get("used", {})},
        "model_switches": records.get("model_switches", []),
        "ablation_blocked": records.get("ablation_blocked"),
        "regression_blocked": records.get("regression_blocked"),
        "probe": records.get("probe", {}),
        "cross_reviews": records.get("cross_reviews", {}),
        "not_run": records.get("not_run", {}),
        "questions": questions,
    }

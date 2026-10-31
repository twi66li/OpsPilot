"""评测结果接口：读取 night20261031/real_run 的冻结场景与逐题结果（admin 只读）。"""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.deps import AuthUser, require_roles

router = APIRouter(prefix="/api/eval", tags=["eval-results"])

_EVAL_DIR = Path(__file__).resolve().parents[2] / "eval" / "night20261031" / "real_run"


@router.get("/results")
def eval_results(user: AuthUser = Depends(require_roles("admin"))):
    """返回真实评测批次结果：总体完成数、逐题输入/编号证据/答案/双裁判判分/失败/用量。

    数据来源为评测脚本落盘的 results.json 与 scenarios_frozen.json（只读），不提供写接口。
    """
    results_fp = _EVAL_DIR / "results.json"
    if not results_fp.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "评测结果尚未生成（real_run/results.json 不存在）")
    records = json.loads(results_fp.read_text(encoding="utf-8"))
    frozen_fp = _EVAL_DIR / "scenarios_frozen.json"
    frozen = json.loads(frozen_fp.read_text(encoding="utf-8")) if frozen_fp.exists() else {}
    by_id = {s["id"]: s for s in frozen.get("scenarios", [])}
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
            "required_points": rec.get("required_points") or s.get("required_points", []),
            "answer": rec.get("answer", ""),
            "citations": rec.get("citations", []),
            "evidence": rec.get("evidence", []),
            "judges": {m: {"verdict": v.get("verdict"), "missing": v.get("missing", True),
                           "usage": v.get("usage", {})} for m, v in judges.items()},
            "usage": rec.get("usage", {}),
            "error": rec.get("error") or (rec.get("attempts") or None),
            "elapsed_s": rec.get("elapsed_s"),
        })
    return {
        "namespace": records.get("namespace"),
        "batch": records.get("batch"),
        "config": records.get("config", {}),
        "summary": records.get("summary", {}),
        "manifest": records.get("manifest", {}),
        "probe": records.get("probe", {}),
        "questions": questions,
    }

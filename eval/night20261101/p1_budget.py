"""本轮（run-20261101）P1 份额共享核算。

口径（budget.json）：
- 新预算只计本轮启动后的新增调用；本轮账本为全新文件，全量行均属本轮；
- 快照前历史实报不重复扣除；embedding 历史未知预留 26,364 单列保留（不计入新增份额）；
- 评测、工作台、后台调用合并计数：各进程持有各自账本（单写者锁按路径），
  启动时按"份额 − 兄弟账本已用"计算本进程生效上限，避免重复领取完整份额；
- 跨账本在途预留互不可见，靠并发纪律（本轮项目并发 1：批跑期间不做工作台对话）兜底并披露。
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
BUDGET_JSON = Path("D:/agent-hub/run-20261101/budget.json")

# 与 budget.json project_total_limits.p1 / models.*.p1 一致；文件缺失时兜底
FALLBACK_SHARES = {
    "qwen3.8-max-0902": 160000,
    "qwen3.8-27b": 100000,
    "deepseek-v4-pro-0813": 40000,
    "qwen3.7-text-embedding-flash": 10000,
    "qwen3.7-text-rerank": 220000,
}
EMBEDDING_UNKNOWN_RESERVE = 26364  # 历史未知预留，单列保留核对，不扣减新增份额

# 2026-11-01 终审整改追加授权：用户在会话内以选项明确"全额授权"（AskUserQuestion 记录），
# 用于审查验收的真实 API 验证（重跑 12 道受影响题含 reg3、金标复判、工作台典型流程演示）。
# 追加到 budget.json p1 列之上，仅本轮有效；实报全部走本轮账本。
# 第二次全额授权（p1-final-02 整改）：0902 +120K / 27b +45K / rerank +85K，
# 用于受残行影响 6 题重跑、reg-002/003、缺判补齐与工作台核心流程。
# 第三次授权（判分收尾）：0902 +8K / 27b +26K，仅补 3 个缺判（无生成消耗）。
# 第四次授权（最后一判）：0902 +6K，reg-003 主判（生效上限需扣工作台账本 12,839）。
REVIEW_FIX_GRANTS = {
    "qwen3.8-max-0902": 130000 + 120000 + 8000 + 6000,
    "qwen3.8-27b": 15000 + 45000 + 26000,
    "qwen3.7-text-rerank": 65000 + 85000,
}


def p1_shares() -> dict[str, int]:
    try:
        budget = json.loads(BUDGET_JSON.read_text(encoding="utf-8"))
        shares = {m: int(cfg["p1"]) for m, cfg in budget["models"].items() if int(cfg.get("p1", 0)) > 0}
        if shares:
            for m, g in REVIEW_FIX_GRANTS.items():
                shares[m] = shares.get(m, 0) + g
            return shares
    except (OSError, KeyError, ValueError):
        pass
    shares = dict(FALLBACK_SHARES)
    for m, g in REVIEW_FIX_GRANTS.items():
        shares[m] = shares.get(m, 0) + g
    return shares


def ledger_usage(path: str | Path) -> dict[str, dict[str, int]]:
    """重放单本账本，返回逐模型 {actual, unknown, in_flight}（与本夜 ledger_history 同口径：预留按 ID 配对结算）。"""
    usage: dict[str, dict[str, int]] = {}
    fp = Path(path)
    if not fp.exists():
        return usage
    settled: set[str] = set()
    rows = [json.loads(l) for l in fp.read_text(encoding="utf-8").splitlines() if l.strip()]
    for r in rows:
        if r.get("type") in ("committed", "committed_unknown"):
            settled.add(r.get("id", ""))
    for r in rows:
        model = r.get("model", "?")
        bucket = usage.setdefault(model, {"actual": 0, "unknown": 0, "in_flight": 0})
        t = r.get("type")
        if t == "committed":
            bucket["actual"] += int(r.get("actual", 0))
        elif t == "committed_unknown":
            bucket["unknown"] += int(r.get("estimate", 0))
        elif t == "reserved" and r.get("id") not in settled:
            bucket["in_flight"] += int(r.get("estimate", 0))
    return usage


def effective_limits(own_ledger: str | Path, sibling_ledgers: list[str | Path]) -> tuple[dict[str, int], int]:
    """本进程生效上限 = p1 份额 − 兄弟账本已用（actual+unknown+在途）。

    被兄弟耗尽的模型从授权清单剔除（到达供应商前拒绝），剩余模型不足 1 token 时同样剔除。
    返回 (model_limits, total_limit)；total 为剩余模型生效上限之和。
    """
    shares = p1_shares()
    sibling_used: dict[str, int] = {}
    for lp in sibling_ledgers:
        for model, u in ledger_usage(lp).items():
            sibling_used[model] = sibling_used.get(model, 0) + u["actual"] + u["unknown"] + u["in_flight"]
    limits = {}
    for model, share in shares.items():
        remain = share - sibling_used.get(model, 0)
        if remain >= 1:
            limits[model] = remain
    return limits, sum(limits.values())


def remaining_of(path: str | Path) -> dict[str, int]:
    """本账本视角的逐模型剩余（份额 − 本账本已用），供批跑预检门控使用。"""
    shares = p1_shares()
    used = {m: u["actual"] + u["unknown"] + u["in_flight"] for m, u in ledger_usage(path).items()}
    return {m: max(0, share - used.get(m, 0)) for m, share in shares.items()}


def round_status(ledgers: list[str | Path]) -> dict:
    """合并状态：逐账本 + 合并用量 vs 份额（review.json 与工作台账目展示共用）。"""
    shares = p1_shares()
    per_ledger = {str(lp): ledger_usage(lp) for lp in ledgers}
    merged: dict[str, dict[str, int]] = {}
    for u in per_ledger.values():
        for model, b in u.items():
            bucket = merged.setdefault(model, {"actual": 0, "unknown": 0, "in_flight": 0})
            for k in bucket:
                bucket[k] += b[k]
    status = {"shares": shares, "per_ledger": per_ledger, "merged": merged,
              "embedding_unknown_reserve": EMBEDDING_UNKNOWN_RESERVE, "models_remaining": {}}
    for model, share in shares.items():
        b = merged.get(model, {"actual": 0, "unknown": 0, "in_flight": 0})
        status["models_remaining"][model] = share - (b["actual"] + b["unknown"] + b["in_flight"])
    return status


if __name__ == "__main__":
    st = round_status([HERE / "eval_ledger.jsonl", HERE / "workbench" / "ledger.jsonl"])
    print(json.dumps(st, ensure_ascii=False, indent=1))

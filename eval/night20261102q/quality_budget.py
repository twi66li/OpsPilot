"""run-20261102-quality 轮 P1 预算模块。

口径（D:/agent-hub/run-20261102-quality/budget.json）：
- 本轮新增程序 API 上限：P1 项目总额 400,000 tokens；分模型上限见 P1_QUALITY_SHARES
  （分模型之和 > 总额是为了允许替代，不能相加成更大额度；项目总额与分模型上限同时约束）；
- 新余额快照已含过去实际扣减，本轮账本（本目录 ledger.jsonl）只计启动后新调用，
  工作台与评测共用同一份权威账本；旧账本以 baseline.json 冻结为审计起点；
- P1 历史 embedding 未知预留 26,364 单列保留；
- 预留口径：输入侧按 UTF-8 字节做保守工程估算（对所有主流 BPE 类分词器足够保守，
  但不宣称对所有模型是数学证明）；输出侧按 API 实际 max_tokens 强制上限计入；
  可能产生可计费推理 tokens 的模型按模型配置加 reasoning_cap，无法关闭思考的模型如实标记；
- usage 缺失记 unknown 不虚记零；不把剩余额度当供应商实时余额，客户端消耗未知不记零。
"""
import json
import os
from pathlib import Path

HERE = Path(__file__).parent
BUDGET_JSON = Path("D:/agent-hub/run-20261102-quality/budget.json")
LEDGER = HERE / "ledger.jsonl"
BASELINE = HERE / "baseline.json"

FALLBACK_SHARES = {
    "qwen3.8-27b": 120000,
    "qwen3.8-max-0902": 30000,
    "deepseek-v4-pro-0813": 80000,
    "deepseek-v4.1-flash": 70000,
    "deepseek-v4.1-flash-1017": 70000,
    "qwen3.8-2.4t-a95b": 80000,
    "kimi-k3": 70000,
    "glm-5.3": 70000,
    "qwen3.8-flash": 20000,
    "qwen3.7-text-embedding-flash": 10000,
    "qwen3.7-text-rerank": 60000,
}
TOTAL_CAP = 400000
EMBEDDING_UNKNOWN_RESERVE = 26364

# 分模型预留配置：output_cap 为请求实际携带并被服务端强制（或按供应商口径可确认）的
# 输出上限；reasoning_cap 为可能产生可计费推理 tokens 时额外预留的保守值。
# thinking_disabled=True 表示请求显式关闭思考（如 DashScope enable_thinking=False），
# 此时 reasoning_cap 记 0。
MODEL_RESERVE_CONFIG = {
    "qwen3.8-27b": {"output_cap": 4096, "reasoning_cap": 0, "thinking_disabled": True,
                    "note": "DashScope enable_thinking=False 实测生效（探针 reasoning_tokens=null，既往批次一致）"},
    "qwen3.8-max-0902": {"output_cap": 1536, "reasoning_cap": 0, "thinking_disabled": True, "note": "同上"},
    "qwen3.8-flash": {"output_cap": 1536, "reasoning_cap": 0, "thinking_disabled": True, "note": "轻量可选"},
    "deepseek-v4-pro-0813": {"output_cap": 1536, "reasoning_cap": 0, "thinking_disabled": True,
                             "note": "探针：enable_thinking 参数被接受，usage 无 reasoning 拆分（completion_tokens_details=null）"},
    "deepseek-v4.1-flash": {"output_cap": 1536, "reasoning_cap": 1024, "thinking_disabled": "probe", "note": "未使用，保守预留"},
    "deepseek-v4.1-flash-1017": {"output_cap": 1536, "reasoning_cap": 1024, "thinking_disabled": "probe", "note": "未使用，保守预留"},
    "qwen3.8-2.4t-a95b": {"output_cap": 1536, "reasoning_cap": 4096, "thinking_disabled": False, "note": "探针：enable_thinking 强制 True（不可关闭），usage 返回 reasoning_tokens=38；预留含推理 4096"},
    "kimi-k3": {"output_cap": 1536, "reasoning_cap": 0, "thinking_disabled": True,
                "note": "探针：参数被接受，usage.reasoning_tokens=0（单次观察；若后续出现 reasoning 拆分，结算按实报并 halt 预留超限，fail-safe 兜底）"},
    "glm-5.3": {"output_cap": 1536, "reasoning_cap": 1024, "thinking_disabled": "probe", "note": "未使用，保守预留"},
    "qwen3.7-text-embedding-flash": {"output_cap": 0, "reasoning_cap": 0, "thinking_disabled": True, "note": "embedding"},
    "qwen3.7-text-rerank": {"output_cap": 64, "reasoning_cap": 0, "thinking_disabled": True, "note": "rerank"},
}

GENERATION_ORDER = ["qwen3.8-27b", "qwen3.8-2.4t-a95b", "deepseek-v4.1-flash",
                    "deepseek-v4.1-flash-1017", "kimi-k3", "glm-5.3"]


def p1_shares() -> dict[str, int]:
    try:
        budget = json.loads(BUDGET_JSON.read_text(encoding="utf-8"))
        shares = {m: int(cfg) for m, cfg in budget["model_limits"]["p1"].items() if int(cfg) > 0}
        if shares:
            return shares
    except (OSError, KeyError, ValueError):
        pass
    return dict(FALLBACK_SHARES)


def reserve_for(model: str, payload_bytes: int, output_cap: int | None = None) -> int:
    """单次调用预留：输入保守估算 + 输出上限 + 推理预留（按模型配置）。"""
    cfg = MODEL_RESERVE_CONFIG.get(model, {"output_cap": 1536, "reasoning_cap": 1024, "thinking_disabled": "unknown"})
    cap = cfg["output_cap"] if output_cap is None else max(output_cap, 0)
    return int(payload_bytes) + int(cap) + int(cfg.get("reasoning_cap", 0))


def usage_now() -> dict:
    """本账本累计（重放）：actual/unknown/in_flight 与剩余。"""
    shares = p1_shares()
    used = {m: {"actual": 0, "unknown": 0, "in_flight": 0} for m in shares}
    settled: set[str] = set()
    rows = []
    if LEDGER.exists():
        rows = [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]
        settled = {r.get("id") for r in rows if r.get("type") in ("committed", "committed_unknown")}
    for r in rows:
        m = r.get("model", "?")
        b = used.setdefault(m, {"actual": 0, "unknown": 0, "in_flight": 0})
        if r.get("type") == "committed":
            b["actual"] += r.get("actual", 0)
        elif r.get("type") == "committed_unknown":
            b["unknown"] += r.get("estimate", 0)
        elif r.get("type") == "reserved" and r.get("id") not in settled:
            b["in_flight"] += r.get("estimate", 0)
    total = sum(b["actual"] + b["unknown"] + b["in_flight"] for b in used.values())
    remaining = {m: max(0, s - (b["actual"] + b["unknown"] + b["in_flight"]))
                 for m, s in shares.items() for b in [used.get(m, {"actual": 0, "unknown": 0, "in_flight": 0})]}
    return {"used": used, "remaining": remaining, "project_total_used": total,
            "project_total_cap": TOTAL_CAP, "project_total_remaining": max(0, TOTAL_CAP - total),
            "historical_embedding_unknown_reserve": EMBEDDING_UNKNOWN_RESERVE}


def env_for_launcher(exclude_model_caps: dict[str, int] | None = None) -> dict:
    """给启动器（工作台/评测）的预算环境：同一权威账本 + 本轮份额。

    exclude_model_caps：同进程内已按其他模型扣减的预留调整（当前不需要，预留接口）。
    """
    shares = p1_shares()
    return {
        "BUDGET_LEDGER_PATH": str(LEDGER),
        "BUDGET_TOKEN_LIMIT": str(TOTAL_CAP),
        "BUDGET_MODEL_LIMITS": json.dumps(shares),
    }


def apply_env(env: dict | None = None) -> None:
    env = env or env_for_launcher()
    for k, v in env.items():
        os.environ[k] = v


if __name__ == "__main__":
    print(json.dumps(usage_now(), ensure_ascii=False, indent=1))

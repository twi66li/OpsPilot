"""启动 8001 诊断工作台（run-20261101 新一轮：真实重排链路 + 新 P1 份额）。

用法：.venv/Scripts/python.exe scripts/start_workbench_8001.py
预算：budget.json p1 份额，减去本轮评测账本（eval/night20261101/eval_ledger.jsonl）已用后
作为本进程生效上限；工作台自身调用记入本轮新账本 workbench/ledger.jsonl（合并核算，
不与旧 workbench_run/ 历史账本混算）。批跑评测期间不要通过工作台发起对话（项目并发 1）。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "eval" / "night20261101" / "workbench"
RUN.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval" / "night20261101"))

from p1_budget import effective_limits  # noqa: E402

MODEL_LIMITS, TOTAL_LIMIT = effective_limits(
    RUN / "ledger.jsonl", [ROOT / "eval" / "night20261101" / "eval_ledger.jsonl"])

os.environ["DATABASE_URL"] = "mysql+pymysql://aiops:aiops123@127.0.0.1:13306/aiops?charset=utf8mb4"
os.environ["QDRANT_MODE"] = "remote"
os.environ["QDRANT_URL"] = "http://127.0.0.1:6333"
os.environ["QDRANT_COLLECTION"] = "kb_qwen37_1024_v2"
os.environ["EMBEDDING_PROVIDER"] = "openai"
os.environ["EMBEDDING_DIM"] = "1024"
os.environ["EMBEDDING_MODEL"] = "qwen3.7-text-embedding-flash"
os.environ["LLM_PROVIDER"] = "openai"
os.environ["MODEL_STRICT"] = "true"
os.environ["CHAT_MODEL"] = "qwen3.8-max-0902"
os.environ["RERANK_ENABLED"] = "true"  # 本轮授权恢复真实重排（qwen3.7-text-rerank，p1 份额 220K）
os.environ["RERANK_PROVIDER"] = "dashscope"
os.environ["RERANK_MODEL"] = "qwen3.7-text-rerank"
os.environ["BUDGET_LEDGER_PATH"] = str(RUN / "ledger.jsonl")
os.environ["BUDGET_TOKEN_LIMIT"] = str(TOTAL_LIMIT)
os.environ["BUDGET_MODEL_LIMITS"] = json.dumps(MODEL_LIMITS)

import uvicorn  # noqa: E402

if __name__ == "__main__":
    (RUN / "limits.json").write_text(json.dumps({
        "effective_model_limits": MODEL_LIMITS, "effective_total_limit": TOTAL_LIMIT,
        "note": "budget.json p1 份额 − 评测账本已用；启动时快照，运行中以账本为准",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8001)

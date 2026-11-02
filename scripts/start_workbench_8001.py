"""启动 8001 诊断工作台。

用法：.venv/Scripts/python.exe scripts/start_workbench_8001.py
预算：run-20261102-quality 轮权威账本（eval/night20261102q/ledger.jsonl，与评测共用同一份；
P1 项目总额 400K + run-20261102-quality/budget.json 分模型上限同时约束）。
旧账本已按 eval/night20261102q/baseline.json 冻结为审计起点，不得与新份额同时领取。
生成模型：qwen3.8-27b（budget.json generation_order 首选）。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QDIR = ROOT / "eval" / "night20261102q"
RUN = QDIR / "workbench"
RUN.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(QDIR))

from quality_budget import apply_env  # noqa: E402

apply_env()  # 本轮权威账本 + P1 总额/分模型上限（评测与工作台共用，严禁双领）

os.environ["DATABASE_URL"] = "mysql+pymysql://aiops:aiops123@127.0.0.1:13306/aiops?charset=utf8mb4"
os.environ["QDRANT_MODE"] = "remote"
os.environ["QDRANT_URL"] = "http://127.0.0.1:6333"
os.environ["QDRANT_COLLECTION"] = "kb_qwen37_1024_v2"
os.environ["EMBEDDING_PROVIDER"] = "openai"
os.environ["EMBEDDING_DIM"] = "1024"
os.environ["EMBEDDING_MODEL"] = "qwen3.7-text-embedding-flash"
os.environ["LLM_PROVIDER"] = "openai"
os.environ["MODEL_STRICT"] = "true"
os.environ["CHAT_MODEL"] = "qwen3.8-27b"
os.environ["RERANK_ENABLED"] = "true"
os.environ["RERANK_PROVIDER"] = "dashscope"
os.environ["RERANK_MODEL"] = "qwen3.7-text-rerank"
os.environ["AGENT_MODE"] = "rule"

import uvicorn  # noqa: E402

if __name__ == "__main__":
    (RUN / "limits.json").write_text(json.dumps({
        "ledger": os.environ["BUDGET_LEDGER_PATH"],
        "project_total_cap": os.environ["BUDGET_TOKEN_LIMIT"],
        "model_limits": json.loads(os.environ["BUDGET_MODEL_LIMITS"]),
        "note": "run-20261102-quality 轮账本；工作台与评测共用同一权威账本，启动时快照，运行中以账本为准",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8001)

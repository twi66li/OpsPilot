"""启动 8001 诊断工作台（独立预算账本，硬上限 = p1 各槽剩余）。

用法：.venv/Scripts/python.exe scripts/start_workbench_8001.py
预算口径：p1 累计额度 − 历史 − 评测批次实报（见 eval/night20261031/real_run/manifest）。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "eval" / "night20261031" / "workbench_run"
RUN.mkdir(exist_ok=True)

# p1 槽位剩余（2026-11-01 评测批次后核算；qwen3.8-max 槽由 qwen3.8-max-0902 继承）
MODEL_LIMITS = {
    "qwen3.8-max-0902": 6882,
    "qwen3.7-text-embedding-flash": 29034,
    "qwen3.7-text-rerank": 5116,
}

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
os.environ["RERANK_ENABLED"] = "true"
os.environ["RERANK_PROVIDER"] = "dashscope"
os.environ["RERANK_MODEL"] = "qwen3.7-text-rerank"
os.environ["BUDGET_LEDGER_PATH"] = str(RUN / "ledger.jsonl")
os.environ["BUDGET_TOKEN_LIMIT"] = str(sum(MODEL_LIMITS.values()))
os.environ["BUDGET_MODEL_LIMITS"] = json.dumps(MODEL_LIMITS)

sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    (RUN / "limits.json").write_text(json.dumps(MODEL_LIMITS, ensure_ascii=False, indent=1), encoding="utf-8")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8001)

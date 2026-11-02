"""新模型能力探针（P1-QUALITY-20261102）：每个实际用到的新模型一次最小请求。

确认：text 生成、JSON mode、usage 返回（含 reasoning 拆分）、enable_thinking 参数
接受性、finish_reason。结果回填 app/services/model_api.py 的分模型预留配置。
探针消耗计入本轮权威账本（eval/night20261102q/ledger.jsonl）。
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from quality_budget import apply_env, reserve_for  # noqa: E402

apply_env()

os.environ.setdefault("DATABASE_URL", "mysql+pymysql://aiops:aiops123@127.0.0.1:13306/aiops?charset=utf8mb4")
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["LLM_PROVIDER"] = "openai"
os.environ["MODEL_STRICT"] = "true"
# OPENAI_API_KEY / OPENAI_BASE_URL 由项目根 .env 提供（pydantic Settings 读取），无需在此设置

OUT = HERE / "probe_results.json"


def probe(model: str, api_key: str, base_url: str) -> dict:
    from app.services.model_api import ChatAPI

    res = {"model": model}
    api = ChatAPI(api_key, base_url, model)
    # 1) 普通小请求（携带 enable_thinking=False 的既有 extra_body 逻辑，验证接受性）
    try:
        text = api.chat("只回复两个字：好的", "ping", json_mode=False)
        rec = api.calls[-1]
        res["chat"] = {"ok": True, "text_head": (text or "")[:20], "finish": rec.get("finish_reason"),
                       "usage": rec.get("usage")}
    except Exception as exc:
        res["chat"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:220]}
        return res
    # 2) JSON mode
    try:
        text = api.chat('只输出 JSON：{"ok": true}', "ping", json_mode=True)
        rec = api.calls[-1]
        parsed = None
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        res["json_mode"] = {"ok": parsed is not None, "parsed": parsed, "usage": rec.get("usage")}
    except Exception as exc:
        res["json_mode"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:220]}
    # 3) usage 细节：是否有 reasoning/thinking 拆分
    u = (res.get("chat") or {}).get("usage") or {}
    res["usage_fields"] = sorted(u.keys()) if isinstance(u, dict) else []
    res["reasoning_tokens"] = (u or {}).get("completion_tokens_details", {}) if isinstance(u, dict) else None
    return res


def main():
    from app.config import get_settings

    settings = get_settings()
    api_key, base_url = settings.openai_api_key, settings.openai_base_url
    models = sys.argv[1:] or ["qwen3.8-27b", "deepseek-v4-pro-0813", "kimi-k3"]
    results = {}
    if OUT.exists():
        results = json.loads(OUT.read_text(encoding="utf-8"))
    for m in models:
        if m in results and results[m].get("chat", {}).get("ok"):
            print(f"skip {m} (already probed ok)")
            continue
        print(f"probing {m} ...", flush=True)
        results[m] = probe(m, api_key, base_url)
        print(json.dumps(results[m], ensure_ascii=False)[:400], flush=True)
        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("probe results ->", OUT)


if __name__ == "__main__":
    main()

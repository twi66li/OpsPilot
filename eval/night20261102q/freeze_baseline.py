"""冻结本轮（run-20261102-quality）启动基线：旧账本 hash/末事件/累计用量（budget.json ledger_policy 第1条）。

新轮账本只计启动后新调用；旧账本以此快照为审计起点，不再从新余额重复扣减旧实报；
旧 unknown/open 保守保留，P1 历史 embedding 未知预留 26,364 单列。
"""
import glob
import hashlib
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
os.chdir(ROOT)

old_ledgers = {}
patterns = [r"eval/night20261031/**/ledger.jsonl", r"eval/night20261101/**/ledger.jsonl",
            r"eval/night20261101/eval_ledger.jsonl"]
seen = set()
for pat in patterns:
    for p in glob.glob(pat, recursive=True):
        p = p.replace("\\", "/")
        if p in seen or p.endswith(".lock") or not os.path.exists(p):
            continue
        seen.add(p)
        raw = open(p, "rb").read()
        rows = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
        used = {}
        settled = {r["id"] for r in rows if r.get("type") in ("committed", "committed_unknown")}
        for r in rows:
            m = r.get("model", "?")
            b = used.setdefault(m, {"actual": 0, "unknown": 0, "in_flight": 0})
            if r.get("type") == "committed":
                b["actual"] += r.get("actual", 0)
            elif r.get("type") == "committed_unknown":
                b["unknown"] += r.get("estimate", 0)
            elif r.get("type") == "reserved" and r.get("id") not in settled:
                b["in_flight"] += r.get("estimate", 0)
        old_ledgers[p] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "rows": len(rows),
            "last_event_id": rows[-1].get("id") if rows else None,
            "used": used,
        }

baseline = {
    "frozen_at": "2026-11-03T00:00:00+08:00",
    "git_head": "01e2ef92def3cf89220b93334cc90c1ce7405e40",
    "round": "run-20261102-quality",
    "policy": ("新轮账本（本目录 ledger.jsonl）只计启动后新调用；旧账本以此快照为审计起点，"
               "不再从新余额重复扣减旧实报；旧 unknown/open 保守保留，"
               "P1 历史 embedding 未知预留 26364 单列。工作台与评测共用本新轮权威账本。"),
    "old_ledgers": old_ledgers,
    "historical_embedding_unknown_reserve": 26364,
}
out = os.path.join(HERE, "baseline.json")
open(out, "w", encoding="utf-8").write(json.dumps(baseline, ensure_ascii=False, indent=1))
tot = 0
for p, info in old_ledgers.items():
    for m, b in info["used"].items():
        tot += b["actual"] + b["unknown"] + b["in_flight"]
    print(p, "rows", info["rows"], "sha", info["sha256"][:12])
print("old cumulative total (audit reference):", tot)
print("written", out)

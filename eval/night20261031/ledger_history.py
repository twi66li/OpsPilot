"""汇总历史账本占用：实报 + 未知 + 未结算预留（按预留ID配对），对照 p1 上限算余量。"""
import json
from pathlib import Path

P1 = {"qwen3.8-max": 302500, "qwen3.8-27b": 192500, "deepseek-v4-pro-0813": 137500,
      "qwen3.7-text-embedding-flash": 110000, "qwen3.7-text-rerank": 110000}

total: dict[str, dict] = {}
for p in sorted(Path(__file__).parents[2].rglob("ledger*.jsonl")):
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    reserved_ids = {r["id"] for r in rows if r["type"] == "reserved"}
    settled = {r["id"] for r in rows if r["type"] in ("committed", "committed_unknown")}
    unsettled = reserved_ids - settled
    by_model: dict[str, dict] = {}
    for r in rows:
        s = by_model.setdefault(r.get("model", "?"), {"actual": 0, "unknown": 0, "unsettled": 0})
        if r["type"] == "committed":
            s["actual"] += r["actual"]
        elif r["type"] == "committed_unknown":
            s["unknown"] += r["estimate"]
        elif r["type"] == "reserved" and r["id"] in unsettled:
            s["unsettled"] += r["estimate"]
    print("==", p.relative_to(Path(__file__).parents[2]))
    for m, s in sorted(by_model.items()):
        print(f"   {m}: actual={s['actual']} unknown={s['unknown']} unsettled={s['unsettled']}")
        t = total.setdefault(m, {"actual": 0, "unknown": 0, "unsettled": 0})
        for k in t:
            t[k] += s[k]

print("\n== 汇总（p1 口径） ==")
grand = 0
for m, lim in P1.items():
    s = total.get(m, {"actual": 0, "unknown": 0, "unsettled": 0})
    used = s["actual"] + s["unknown"] + s["unsettled"]
    grand += used
    print(f"{m}: p1={lim} used={used} remaining={lim - used}")
print(f"total used={grand}")

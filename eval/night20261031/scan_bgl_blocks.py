"""一次性扫描 BGL.log：按 25 万行分块统计消息模板/级别/告警标签，定位候选日志组窗口。

输出写入 bgl_scan_blocks.json（缓存目录），供场景设计挑选互不重叠窗口使用。
"""
import json
import re
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

CACHE = Path(r"C:/Users/86187/Documents/Codex/2026-10-30/codex-4-codex-reviewer-gpt-6/work/public-data-source-cache")
ZIP = CACHE / "BGL.zip"
OUT = Path(__file__).parent / "bgl_scan_blocks.json"
BLOCK = 250_000

# 数字归一化成占位符，得到消息模板
_NUM = re.compile(r"\d+")


def template(msg: str) -> str:
    return _NUM.sub("<N>", msg)[:120]


def main():
    z = zipfile.ZipFile(ZIP)
    blocks = {}
    with z.open("BGL.log") as f:
        for i, raw in enumerate(f, 1):
            ln = raw.decode("utf-8", errors="replace").rstrip("\n")
            parts = ln.split(" ", 5)
            if len(parts) < 6:
                continue
            label, epoch, date, node, ts, rest = parts
            rest_parts = rest.split(" ", 2)
            node2 = rest_parts[0]
            tail = rest_parts[2] if len(rest_parts) > 2 else ""  # rest[1] 恒为 RAS
            tail_parts = tail.split(" ", 2)
            kind = tail_parts[0] if tail_parts else ""  # KERNEL/APP/LINKCARD/MMCS/...
            level = tail_parts[1] if len(tail_parts) > 1 else ""  # INFO/FATAL/WARNING
            msg = tail_parts[2] if len(tail_parts) > 2 else ""
            b = (i - 1) // BLOCK
            d = blocks.setdefault(b, {"lines": 0, "levels": Counter(), "kinds": Counter(),
                                      "templates": Counter(), "alert_labels": Counter(),
                                      "nodes": set(), "alert_line_examples": [], "info_line_examples": []})
            d["lines"] += 1
            d["levels"][level] += 1
            d["kinds"][kind] += 1
            d["templates"][f"{kind} {level} {template(msg)}"] += 1
            d["nodes"].add(node)
            if label != "-":
                d["alert_labels"][label] += 1
                if len(d["alert_line_examples"]) < 6:
                    d["alert_line_examples"].append({"line": i, "text": ln[:260]})
            elif len(d["info_line_examples"]) < 6 and i % 977 == 0:
                d["info_line_examples"].append({"line": i, "text": ln[:260]})
    out = {}
    for b, d in sorted(blocks.items()):
        out[f"{b * BLOCK + 1}-{(b + 1) * BLOCK}"] = {
            "lines": d["lines"],
            "levels": dict(d["levels"].most_common(12)),
            "kinds": dict(d["kinds"].most_common(8)),
            "top_templates": [(t, c) for t, c in d["templates"].most_common(18)],
            "distinct_templates": len(d["templates"]),
            "alert_labels": dict(d["alert_labels"].most_common(10)),
            "alert_lines_total": sum(d["alert_labels"].values()),
            "distinct_nodes": len(d["nodes"]),
            "sample_nodes": sorted(d["nodes"])[:8],
            "alert_line_examples": d["alert_line_examples"],
            "info_line_examples": d["info_line_examples"],
        }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("blocks:", len(out), "written", OUT)


if __name__ == "__main__":
    main()

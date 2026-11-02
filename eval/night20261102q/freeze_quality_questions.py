"""冻结 P1-QUALITY-20261102 的 12 道新题（4 开发 + 8 留出）与新窗口语料。

来源：BGL.zip 原始公开包（与既往轮次同一数据源），窗口与旧 dev/test/reg 全部窗口
（L14700/L505000/L1510000/L4270000/L500/L253000/L1120000/L3765000 ±200 行）互斥。
新窗口原始日志作为检索语料入库（新租户 n31q）；题干/金标/解题解释不入库。
可程序推导的真值（计数/级别/时间/分组）由本脚本的独立解析断言（不复用 logstats 代码，
避免共享实现盲区）；开放题金标列出有据/不可证实事项。human_reviewed=false。
"""
import json
import zipfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
BGL_ZIP = Path(r"C:\Users\86187\Documents\Codex\2026-10-30\codex-4-codex-reviewer-gpt-6\work\public-data-source-cache\BGL.zip")
OLD_WINDOWS = [500, 253000, 1120000, 3765000, 505000, 1510000, 4270000, 14700]
W = 35


def load_stripped():
    with zipfile.ZipFile(BGL_ZIP) as z:
        with z.open("BGL.log") as f:
            raw = f.read().decode("utf-8", errors="replace").splitlines()
    out = []
    for l in raw:
        parts = l.split(None, 1)
        out.append(parts[1] if len(parts) > 1 else l)
    return out


def wlines(stripped, start, n=W):
    return stripped[start - 1:start - 1 + n]


def level_counts(lines):
    return dict(Counter(l.split()[7] for l in lines if len(l.split()) > 8))


def msg_groups(lines):
    c = Counter(" ".join(l.split()[8:]) for l in lines if len(l.split()) > 8)
    return [{"message": m, "count": n, "line_numbers": [i + 1 for i, l in enumerate(lines)
              if len(l.split()) > 8 and " ".join(l.split()[8:]) == m]} for m, n in c.most_common()]


def time_bounds(lines):
    import re
    ts = [(int(l.split()[0]), l.split()[3]) for l in lines if len(l.split()) > 8 and re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{2}\.\d{2}\.\d{2}\.\d{6}", l.split()[3])]
    lo = min(ts, key=lambda x: x[1]) if ts else None
    hi = max(ts, key=lambda x: x[1]) if ts else None
    return {"first_epoch": lo[0], "first": lo[1], "last_epoch": hi[0], "last": hi[1]} if ts else None


def line_no(start, lines):
    # 语料文档行号 = 原始 BGL 行号（题目直接用原始行号，与既有轮口径一致）
    return [start + i for i in range(len(lines))]


def main():
    stripped = load_stripped()

    # 12 个互斥新窗口（与旧窗 ±200 行不重叠；窗口原始行号即题中行号）
    windows = {
        "dev-q1": 3395430,   # 35×同一 ASSERT ERROR（语义解释+统计组合）
        "dev-q2": 2900000,   # 35×generating core 全不同（语义解释）
        "dev-q3": 3469745,   # FATAL 29 突发、消息多样（描述诊断+消融）
        "dev-q4": 382930,    # INFO 28/FATAL 7 多节点（描述诊断+消融）
        "heldout-q1": 4655990,  # ciod Error loading 分组含并列
        "heldout-q2": 1800000,  # generating core 纯统计（计数+时间范围）
        "heldout-q3": 2934565,  # 节点卡消息三级级别计数、位置字段 UNKNOWN_LOCATION
        "heldout-q4": 3080825,  # ciod LOGIN chdir ×35 同文（计数+分组平凡）
        "heldout-q5": 4206185,  # 节点卡消息（级别计数+描述）
        "heldout-q6": 3014300,  # 缺行设计：文档 35 行，题问 50 行
        "heldout-q7": 530000,   # data storage interrupt/instruction address 分组+根因不可证实
        "heldout-q8": 2500000,  # generating core 计数+时间范围（纯统计）
    }
    for a, b in ((w, v) for w in windows.values() for v in windows.values() if w < v):
        assert b - a >= W, f"窗口重叠：{a} vs {b}"
    for w in windows.values():
        for o in OLD_WINDOWS:
            assert not (w - 35 <= o + 200 and w + W + 50 >= o - 200), f"窗口 {w} 与旧窗 {o} 过近"

    corpus_docs, scenarios = [], []
    for qid, start in windows.items():
        lines = wlines(stripped, start)
        for l in lines:
            assert len(l.split()) > 8, f"窗口 {qid} 行格式异常"
        # 语料行带 "L<原始行号>:" 前缀（与既有语料/证据解析口径一致）
        prefixed = [f"L{start + i}: {l}" for i, l in enumerate(lines)]
        title = f"BGL 质量轮摘录 L{start}-L{start + W - 1}"
        corpus_docs.append({"title": title, "file": f"bglq_{qid}_L{start}_L{start + W - 1}.txt",
                            "start": start, "end": start + W - 1, "lines": prefixed,
                            "raw_lines": lines})
        scenarios.append({"id": qid, "window_start": start, "window_end": start + W - 1,
                          "doc_title": title, "lines": prefixed})

    def split_of(qid):
        return "dev" if qid.startswith("dev") else "heldout"

    def build(qid, sc):
        start, lines, full = sc["window_start"], sc["lines"], line_no(sc["window_start"], sc["lines"])
        nums = "L" + "、L".join(str(n) for n in full)
        rng = f"L{start}-L{start + W - 1}"
        levels = level_counts(lines)
        groups = msg_groups(lines)
        tb = time_bounds(lines)
        g = {"level_counts": levels, "time_bounds": tb,
             "top_messages": groups[:3], "distinct_messages": len(groups),
             "total_lines": len(lines), "line_numbers": full}
        fam = {
            "dev-q1": ("stats_semantic", f"{rng} 内反复出现同一条 idoproxydb ASSERT 错误共 {levels} 条级别记录，这种情况说明什么？需要什么证据才能定位根因？"),
            "dev-q2": ("stats_semantic", f"{rng} 内每行都是一条不同的 generating core 消息，这组日志说明系统发生了什么？如何确认是否异常？"),
            "dev-q3": ("descriptive", f"{rng} 内出现大量 FATAL 级别日志，涉及什么错误类型？能否据此确认根因？请说明哪些结论有据、哪些需要进一步取证。"),
            "dev-q4": ("descriptive", f"{rng} 内 INFO 与 FATAL 消息来自多个不同节点，请概述该时间窗内各节点发生了什么，并指出哪些判断有日志依据、哪些不能确认。"),
            "heldout-q1": ("literal_grouping", f"{rng} 内出现最多的是哪条消息？请按消息正文逐字分组给出频次与来源行号；如有并列请全部列出。"),
            "heldout-q2": ("pure_stats", f"{rng} 内 generating core 消息共多少条？首末两条的精确时间戳（第 5 列）分别是什么？"),
            "heldout-q3": ("level_counts", f"{rng} 内 WARNING、ERROR、SEVERE 级别各有多少条？这些日志的节点/位置字段是什么？"),
            "heldout-q4": ("literal_grouping", f"{rng} 内 FATAL 消息共有多少条？其中 ciod LOGIN chdir 开头的消息出现几次？"),
            "heldout-q5": ("level_counts_desc", f"{rng} 内 WARNING、SEVERE、ERROR 级别各多少条？这组日志主要反映了什么问题？哪些结论有日志依据？"),
            "heldout-q6": ("missing_lines", f"L{start}-L{start + 49} 内 Node card is not fully functional 消息共多少条？请说明统计覆盖范围。"),
            "heldout-q7": ("grouping_rootcause", f"{rng} 内 data storage interrupt 与 instruction address 两类消息各出现多少次？这些错误能否确认是硬件故障？"),
            "heldout-q8": ("pure_stats", f"{rng} 内共有多少条日志？时间范围是什么（第 5 列）？"),
        }[qid]
        # 程序推导真值（独立解析，不复用 logstats）
        if qid == "heldout-q6":
            asked = 50
            covered = W
            g["asked_range"] = {"lo": start, "hi": start + asked - 1, "missing": list(range(start + W, start + asked))}
            g["covered_count"] = sum(1 for l in lines if "Node card is not fully functional" in l)
            g["note"] = f"文档只覆盖 {rng}；缺失 L{start + W}-L{start + asked - 1} 共 {asked - W} 行不得计入总数"
        return {"id": qid, "split": split_of(qid), "family": fam[0], "question": fam[1],
                "window": rng, "window_start": start, "window_end": start + W - 1,
                "gold_program": g, "gold_notes": {
                    "dev-q1": {"有据": ["35 行同文 ASSERT 错误、来源行号", "同一来源组件（消息正文）"],
                               "不可证实": ["根因（需应用日志/配置变更记录/告警系统）"]},
                    "dev-q2": {"有据": ["35 条互不相同的 generating core 消息", "每条含不同 core 文件编号"],
                               "不可证实": ["是否异常（需基线频率对比）", "进程崩溃原因（需应用侧证据）"]},
                    "dev-q3": {"有据": ["FATAL 29 条及消息类型清单", "错误集中于少数节点（由节点字段统计）"],
                               "不可证实": ["根因（需硬件诊断/后续日志）"]},
                    "dev-q4": {"有据": ["INFO/FATAL 的节点分布与消息类型"],
                               "不可证实": ["节点间因果关系"]},
                    "heldout-q1": {"有据": ["逐字分组与并列项"], "不可证实": []},
                    "heldout-q2": {"有据": ["计数与时间范围"], "不可证实": []},
                    "heldout-q3": {"有据": ["三级级别计数", "位置字段原文（UNKNOWN_LOCATION 如实转述）"], "不可证实": ["真实物理位置"]},
                    "heldout-q4": {"有据": ["FATAL 计数与同文消息计数"], "不可证实": ["chdir 失败的应用侧原因"]},
                    "heldout-q5": {"有据": ["三级级别计数与消息内容"], "不可证实": ["节点卡问题的根因"]},
                    "heldout-q6": {"有据": ["已覆盖 35 行的统计与缺失清单"], "不可证实": ["缺失 15 行的内容"]},
                    "heldout-q7": {"有据": ["两类消息计数与行号"], "不可证实": ["硬件故障结论（需硬件诊断证据）"]},
                    "heldout-q8": {"有据": ["计数与时间范围"], "不可证实": []},
                }[qid]}

    scenarios = [build(qid, sc) for qid, sc in [(qid, next(s for s in scenarios if s["id"] == qid)) for qid in windows]]

    # 语料落盘 + manifest（题干/金标不入库）
    cdir = HERE / "corpus"
    cdir.mkdir(exist_ok=True)
    manifest = []
    for d in corpus_docs:
        fp = cdir / d["file"]
        fp.write_text("\n".join(d["lines"]) + "\n", encoding="utf-8")
        manifest.append({"title": d["title"], "file": d["file"], "start": d["start"], "end": d["end"],
                         "bytes": fp.stat().st_size})

    out = {"namespace": "n31q", "batch": "quality_12",
           "corpus_docs": [{"title": d["title"], "file": d["file"]} for d in corpus_docs],
           "corpus_manifest": manifest,
           "questions": [{k: v for k, v in s.items() if k != "lines"} for s in scenarios],
           "notes": {"human_reviewed": False,
                     "gold_derivation": "可程序推导真值由本脚本独立解析断言（未复用 logstats 实现）；开放题金标列出有据/不可证实事项",
                     "window_disjoint": "与旧 dev/test/reg 全部窗口 ±200 行互斥；开发/留出窗口互斥",
                     "corpus_policy": "仅新窗口原始日志入库（新租户 n31q）；题干/金标/解释不入库"}}
    (HERE / "scenarios_frozen.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"frozen {len(scenarios)} questions; corpus docs {len(manifest)}")
    for s in scenarios:
        print(" ", s["id"], s["family"], s["window"])


if __name__ == "__main__":
    main()

"""生成 test-004 / test-030 的金标更正记录（终审 P1-GOLD 整改）。

原则（审查验收条件）：
- 全部更正由 BGL 原始行程序化推导并断言，本脚本可复现；
- 旧冻结集 scenarios_frozen.json 与旧成绩原样保留，更正以带版本记录发布（corrections.json）；
- 只对受错误金标影响的既有答案补判（由 fill_gold_refill 脚本执行，另列更正前后结果）。
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
BASELINE = HERE.parent / "night20261031" / "real_run"


def corpus_lines(name):
    return [l.rstrip("\n") for l in (BASELINE / "corpus" / name).read_text(encoding="utf-8").splitlines() if l.strip()]


def field(line: str, col: int) -> str:
    return line.split()[col]


def main():
    frozen = json.loads((BASELINE / "scenarios_frozen.json").read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in frozen["scenarios"]}
    corrections = []

    # ---- test-004：L505-L510 逐行时间戳（语料第 5 列）----
    testA = corpus_lines("bgl_testA_L500_L534.txt")
    ts = {}
    for line in testA:
        m = re.match(r"L(\d+):", line)
        if m and 505 <= int(m.group(1)) <= 510:
            ts[int(m.group(1))] = field(line, 4)
    assert sorted(ts) == list(range(505, 511)), f"L505-L510 行缺失：{sorted(ts)}"
    assert ts[505] == "2005-06-03-15.44.38.747737", ts[505]
    assert ts[510] == "2005-06-03-15.44.39.557375", ts[510]
    old4 = by_id["test-004"]
    new4 = {
        "question": "L505 到 L510 各行的完整时间戳（形如 2005-06-03-15.44.38.…）分别是什么？",
        "required_points": [
            "逐行给出 L505-L510 的时间戳且数值正确（L505 为 2005-06-03-15.44.38.747737，L510 为 2005-06-03-15.44.39.557375）",
            "时间戳列为日志第 5 列（精确到微秒）",
        ],
    }
    corrections.append({
        "id": "test-004",
        "reason": "金标与题干示例时间戳（2005-06-03-15.42.50.823719）与语料原文不符（generate_scenarios.py 硬编码错误）；语料 L505 实为 2005-06-03-15.44.38.747737",
        "old": {"question": old4["question"], "required_points": old4["required_points"]},
        "new": new4,
        "derivation": {f"L{k}": ts[k] for k in sorted(ts)},
    })

    # ---- test-030：L3765007 精确消息唯一；同形式 2 条；17 条属另一模板 ----
    testD = corpus_lines("bgl_testD_L3765000_L3765034.txt")
    anchor = [l for l in testD if l.startswith("L3765007:")]
    assert len(anchor) == 1 and "iar 001061dc dear 0244c25c" in anchor[0], anchor
    exact = [l for l in testD if "iar 001061dc dear 0244c25c" in l]
    assert len(exact) == 1, len(exact)
    pat_same = re.compile(r"iar [0-9a-f]+dc dear [0-9a-f]+c[0-9a-f]+c\b")
    pat_17 = re.compile(r"iar [0-9a-f]+ dear [0-9a-f]+c[0-9a-f]+ec\b")
    iar_lines = [l for l in testD if re.search(r"\biar [0-9a-f]+ dear [0-9a-f]+", l)]
    n_same = sum(1 for l in iar_lines if pat_same.search(l))
    n_17 = sum(1 for l in iar_lines if pat_17.search(l))
    assert n_same == 2 and n_17 == 17, (n_same, n_17)
    old30 = by_id["test-030"]
    new30 = {
        "question": old30["question"],
        "required_points": [
            old30["required_points"][0],
            old30["required_points"][1],
            ("能区分“精确消息唯一”与“同形式消息”更佳：与 L3765007 同形式（iar <十六进制>dc dear <十六进制>c<十六进制>c）"
             "在窗口内共 2 条；形如 iar 00106228 dear 0244c1ec（iar <十六进制> dear <十六进制>c<十六进制>ec）的另一模板共 17 条，"
             "另有 dear 尾为 c21c 的 16 条——17 条不与 L3765007 同形式，不应混同"),
        ],
    }
    corrections.append({
        "id": "test-030",
        "reason": "金标附加要点把 17 条写成与 L3765007 同形式；实际同形式（iar <N>dc dear <N>c<N>c）共 2 条，17 条属于 iar <N> dear <N>c<N>ec 模板（00106228/0244c1ec）",
        "old": {"question": old30["question"], "required_points": old30["required_points"]},
        "new": new30,
        "derivation": {
            "L3765007 原文": anchor[0],
            "同形式（iar <N>dc dear <N>c<N>c）计数": n_same,
            "iar <N> dear <N>c<N>ec 计数": n_17,
            "其余 iar/dear 行（dear 尾 c21c）": len(iar_lines) - n_same - n_17,
            "精确消息出现次数": len(exact),
        },
    })

    record = {
        "version": "gold_v2_20261101",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "eval/night20261101/make_gold_corrections.py（断言驱动，可复现）",
        "applies_to": ["eval/night20261031/real_run（real_eval_40 基线）", "eval/night20261101/rerank_run（rerank_restore_20）"],
        "policy": "旧冻结集与旧成绩原样保留；受影响既有答案按本更正补判，更正前后结果分列",
        "corrections": corrections,
    }
    out = HERE / "gold_corrections" / "corrections.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"written {out} with {len(corrections)} corrections (all assertions passed)")


if __name__ == "__main__":
    sys.exit(main())

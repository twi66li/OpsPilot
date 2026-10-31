"""探查候选 35 行窗口的模板构成，供 dev/test 日志组挑选（只读，不调 API）。"""
import re
import zipfile
from collections import Counter
from pathlib import Path

CACHE = Path(r"C:/Users/86187/Documents/Codex/2026-10-30/codex-4-codex-reviewer-gpt-6/work/public-data-source-cache")
ZIP = CACHE / "BGL.zip"
_CANDIDATES = [500, 253000, 505000, 780000, 1120000, 1510000, 1760000, 1960000,
               2760000, 3050000, 3300000, 3520000, 3765000, 4010000, 4270000, 4550000]
_LEN = 35
_NUM = re.compile(r"\d+")


def lines_at(z, start, length=_LEN):
    out = []
    with z.open("BGL.log") as f:
        for i, raw in enumerate(f, 1):
            if i > start + length - 1:
                break
            if i >= start:
                out.append((i, raw.decode("utf-8", errors="replace").rstrip("\n")))
    return out


def compose(rows):
    c = Counter()
    for i, ln in rows:
        tag, rest = (ln.split(" ", 1) + [""])[:2]
        parts = rest.split(" ", 5)
        # rest: epoch date node ts node2 RAS KIND LEVEL msg...
        tail = parts[5] if len(parts) > 5 else ""
        tp = tail.split(" ", 3)
        kind = tp[0] if tp else "?"
        level = tp[1] if len(tp) > 1 else "?"
        msg = tp[3] if len(tp) > 3 else (tp[2] if len(tp) > 2 else "")
        body = _NUM.sub("<N>", msg)[:56]
        c[(tag if tag != "-" else "info", f"{kind} {level} {body}")] += 1
    return c


def main():
    z = zipfile.ZipFile(ZIP)
    for start in _CANDIDATES:
        rows = lines_at(z, start)
        if len(rows) < _LEN:
            print(f"== L{start}+ TOO SHORT ({len(rows)})")
            continue
        c = compose(rows)
        print(f"== L{start}-L{start + _LEN - 1} ({len(rows)} lines)")
        for (tag, t), n in c.most_common(8):
            print(f"   {n:3d}x [{tag[:7]:7s}] {t}")
        print()


if __name__ == "__main__":
    main()

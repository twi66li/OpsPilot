"""证据组装：把检索命中整理成行边界完整、范围查询可覆盖的证据列表（终审 P1-EVIDENCE 整改）。

要点：
- BGL 语料行带 "L<行号>:" 前缀；分块按字符切分会产生块首/块尾残行（上一块的行尾延续），
  摘录前先剥离块首残行、在行边界收口块尾；
- 用户问题引用显式行号/行范围（如 L14738-L14742）时，从命中分块全文提取完整目标行，
  置于证据首位（chunk_id 标记为 range_assembly）；命中分块不全时回退租户知识库文档
  （按标题中的行范围定位，内容来自租户自有知识库，不引入评测金标）；
- 输出的 items 即生成器实际收到的编号证据，评测记录与裁判使用同一份。
"""
import re

from app.agent.llm import evidence_excerpt  # noqa: F401  (re-export 供既有调用方)

# 语料日志行前缀：L<行号>:
LINE_PREFIX = re.compile(r"(?:^|\n)\s*(L\d{3,8}):")
# 问题中的行号/行范围引用：L505、L14738-L14742、L505 到 L510
_RANGE_SEP = r"(?:到|至|–|—|~|-(?!\d)|\s)"


def parse_line_targets(question: str) -> list[int]:
    """解析问题中的显式行号引用，展开为去重排序的行号集合（异常大范围只取起点，防失控）。"""
    targets: set[int] = set()
    for m in re.finditer(r"L(\d{3,8})(?:\s*" + _RANGE_SEP + r"\s*L?(\d{3,8}))?", question or ""):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        if hi < lo:
            lo, hi = hi, lo
        if hi - lo > 400:
            targets.add(lo)
        else:
            targets.update(range(lo, hi + 1))
    return sorted(targets)


def _is_log_text(text: str) -> bool:
    """判定是否行号前缀式日志文本（BGL 语料），仅对这类文本做块首残行剥离。"""
    return bool(LINE_PREFIX.search(text or ""))


def strip_partial_head(text: str) -> str:
    """剥离块首残行：日志文本的首行若无行号前缀，说明是上一分块末行的延续，整行舍去。"""
    if not _is_log_text(text):
        return text
    lines = text.splitlines()
    if len(lines) > 1 and not LINE_PREFIX.match(lines[0].strip()[:16] or ""):
        lines = lines[1:]
    return "\n".join(lines)


def excerpt_hit(hit_text: str, limit: int) -> str:
    """单个命中分块的摘录：先剥离块首残行，再按行边界收口到 limit。"""
    return evidence_excerpt(strip_partial_head(hit_text or ""), limit)


def _index_lines(text: str) -> dict[int, tuple[str, bool]]:
    """索引文本中的行号前缀行，返回 {行号: (行文本, 是否完整)}。

    完整性判定：该行之后还有换行符则为完整；分块末尾无换行的行是被旧分块按字符
    切断的残行（终审 P1-EVIDENCE），不能当作完整证据使用。
    """
    out: dict[int, tuple[str, bool]] = {}
    if not text:
        return out
    segments = text.split("\n")
    for i, seg in enumerate(segments):
        m = re.match(r"\s*(L\d{3,8}):", seg)
        if m:
            out.setdefault(int(m.group(1)[1:]), (seg.rstrip(), i < len(segments) - 1))
    return out


def _doc_title_covers(title: str, lineno: int) -> bool:
    m = re.search(r"L(\d+)[-–~]L(\d+)", title or "")
    if not m:
        return False
    return int(m.group(1)) <= lineno <= int(m.group(2))


def _resolve_target_lines(targets: list[int], hits: list[dict], db, tenant_id: str) -> tuple[dict[int, tuple[str, str]], list[int]]:
    """解析目标行的权威内容：优先租户文档完整行（按标题行范围定位），
    回退命中块中出现过的完整行；仅残行可用时取残行并计入 incomplete（终审 P1-EVIDENCE：
    只出现行号不等于内容完整，残行不得压过完整原文）。"""
    found: dict[int, tuple[str, str]] = {}
    incomplete: list[int] = []
    if db is not None and tenant_id:
        from app.db.models import Document

        for doc in db.query(Document).filter(Document.tenant_id == tenant_id).all():
            wanted = [t for t in targets if t not in found and _doc_title_covers(doc.title, t)]
            if not wanted:
                continue
            doc_lines = _index_lines(doc.content or "")
            # 文档内容为权威源：其行均为完整行（语料文件以换行结尾）
            for lineno, (line, _complete) in doc_lines.items():
                if lineno in wanted:
                    found[lineno] = (doc.title, line)
    # 文档未能覆盖的目标：从命中块取完整行；只有残行时取最长残行并标记
    hits_occurrences: dict[int, list[tuple[str, str, bool]]] = {}
    for h in hits or []:
        title = h.get("doc_title", "")
        for lineno, (line, complete) in _index_lines(h.get("text", "")).items():
            hits_occurrences.setdefault(lineno, []).append((title, line, complete))
    for t in targets:
        if t in found:
            continue
        occs = hits_occurrences.get(t, [])
        if not occs:
            continue
        fulls = [o for o in occs if o[2]]
        if fulls:
            found[t] = (fulls[0][0], fulls[0][1])
        else:
            best = max(occs, key=lambda o: len(o[1]))
            found[t] = (best[0], best[1])
            incomplete.append(t)
    return found, incomplete


def assemble_evidence(hits: list[dict], question: str, db=None, tenant_id: str = "",
                      per_hit_chars: int = 500, range_chars: int = 12000) -> tuple[list[dict], dict]:
    """生成器实际使用的编号证据：可选的范围完整摘录置首，其余为行边界安全的分块摘录。

    返回 (items, range_meta)。item 字段与评测记录的 evidence 一致
    （n/doc_title/chunk_id/score/text），范围项额外带 assembled/targets_found/targets_missing。
    range_chars 是防御上限而非目标大小：BGL 35 行窗口 ≈5,300 字符、最大 61 行窗口 ≈9,200
    字符，完整计数类问题（终审 test-001/reg-002/003）必须整窗可见；超限整行截断并如实
    报 targets_dropped_by_budget，不静默。
    目标行内容以租户文档完整行为权威（终审 P1-EVIDENCE：块尾残行不得当作完整行），
    无法取得完整行的目标计入 targets_incomplete 并如实透出。
    """
    items: list[dict] = []
    targets = parse_line_targets(question)
    range_meta: dict = {"requested": bool(targets), "targets": len(targets), "assembled": False}
    if targets:
        found, incomplete = _resolve_target_lines(targets, hits, db, tenant_id)
        if found:
            picked = [t for t in targets if t in found]
            lines, used_titles = [], []
            for t in picked:
                title, line = found[t]
                lines.append(line)
                if title not in used_titles:
                    used_titles.append(title)
            # 行数上限换算：整行保留，超限整行截断（不切半行）
            kept, size = [], 0
            for line in lines:
                if size + len(line) + 1 > range_chars and kept:
                    break
                kept.append(line)
                size += len(line) + 1
            dropped = len(lines) - len(kept)
            items.append({
                "doc_title": used_titles[0] if len(used_titles) == 1 else "；".join(used_titles[:3]),
                "chunk_id": "range_assembly",
                "score": None,
                "text": "\n".join(kept),
                "assembled": True,
                "targets_found": len(picked),
                "targets_missing": len(targets) - len(picked),
                "targets_incomplete": len(incomplete),
                "targets_dropped_by_budget": dropped,
            })
            range_meta.update({"assembled": True, "targets_found": len(picked),
                               "targets_missing": len(targets) - len(picked),
                               "targets_incomplete": len(incomplete),
                               "targets_dropped_by_budget": dropped})
    for h in hits or []:
        items.append({
            "doc_title": h.get("doc_title", ""),
            "chunk_id": h.get("chunk_id", ""),
            "score": h.get("score"),
            "text": excerpt_hit(h.get("text", ""), per_hit_chars),
        })
    for i, it in enumerate(items, 1):
        it["n"] = i
    return items, range_meta

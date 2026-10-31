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


def _index_lines(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for m in LINE_PREFIX.finditer(text or ""):
        lineno = int(m.group(1)[1:])
        line_start = m.start(1)
        line_end = text.find("\n", line_start)
        out.setdefault(lineno, text[line_start:line_end if line_end != -1 else len(text)].rstrip())
    return out


def _doc_title_covers(title: str, lineno: int) -> bool:
    m = re.search(r"L(\d+)[-–~]L(\d+)", title or "")
    if not m:
        return False
    return int(m.group(1)) <= lineno <= int(m.group(2))


def assemble_evidence(hits: list[dict], question: str, db=None, tenant_id: str = "",
                      per_hit_chars: int = 500, range_chars: int = 12000) -> tuple[list[dict], dict]:
    """生成器实际使用的编号证据：可选的范围完整摘录置首，其余为行边界安全的分块摘录。

    返回 (items, range_meta)。item 字段与评测记录的 evidence 一致
    （n/doc_title/chunk_id/score/text），范围项额外带 assembled/targets_found/targets_missing。
    range_chars 是防御上限而非目标大小：BGL 35 行窗口 ≈5,300 字符、最大 61 行窗口 ≈9,200
    字符，完整计数类问题（终审 test-001/reg-002/003）必须整窗可见；超限整行截断并如实
    报 targets_dropped_by_budget，不静默。
    """
    items: list[dict] = []
    targets = parse_line_targets(question)
    range_meta: dict = {"requested": bool(targets), "targets": len(targets), "assembled": False}
    if targets:
        found: dict[int, tuple[str, str]] = {}
        for h in hits or []:
            title = h.get("doc_title", "")
            for lineno, line in _index_lines(h.get("text", "")).items():
                if lineno in targets:
                    found.setdefault(lineno, (title, line))
        missing = [t for t in targets if t not in found]
        if missing and db is not None and tenant_id:
            from app.db.models import Document

            for doc in db.query(Document).filter(Document.tenant_id == tenant_id).all():
                still = [t for t in missing if t not in found]
                if not still or not _doc_title_covers(doc.title, still[0]):
                    continue
                for lineno, line in _index_lines(doc.content or "").items():
                    if lineno in targets and lineno not in found:
                        found[lineno] = (doc.title, line)
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
                "targets_dropped_by_budget": dropped,
            })
            range_meta.update({"assembled": True, "targets_found": len(picked),
                               "targets_missing": len(targets) - len(picked),
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

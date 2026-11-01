"""确定性日志统计（P1-ARAG-20261102）：显式行范围 + 可解析条件的程序化计数。

原则（任务书）：
- 明确行范围与已支持条件的计数、时间范围由程序计算并给出统计口径与来源行号，
  模型依据结果解释，不再自行心算；
- 只支持与当前需求有关的条件（行内精确子串、级别字段、时间范围、总行数），
  无法确定性解析的诉求（如"出现最多"的归一化口径）列入 unsupported，向用户澄清，不猜口径；
- 统计只计入完整行，块尾残行不计入并单独报告；同一行号只计一次；
- 不得硬编码题号、金标计数或评测参考答案。
"""
import re

from app.rag.evidence import _resolve_target_lines, parse_line_targets, tenant_docs

# BGL 行格式（去掉 "L<行号>:" 前缀后）：
# 0=epoch 1=日期 2=节点 3=精确时间戳(第5列) 4=节点2 5=RAS 6=KIND 7=LEVEL 8+=消息
_LEVEL_WORDS = ("FATAL", "ERROR", "WARNING", "SEVERE", "CRITICAL", "INFO", "NOTICE", "TRACE")
_COUNT_CUE = re.compile(r"多少条|多少次|共多少|几条|总数|计数|出现了?几次")
_TIME_CUE = re.compile(r"时间戳|时间范围|最早|最晚|第一条|最后一条|起始|结束")
_MODE_CUE = re.compile(r"出现最多|最常见|最多的消息|分布")
_ASCII_SEQ = re.compile(r"[A-Za-z][A-Za-z0-9_./-]*(?:\s+[A-Za-z][A-Za-z0-9_./-]*)+")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}-\d{2}\.\d{2}\.\d{2}\.\d{6}")

BASIS = ("统计口径：仅计入完整行；行内容以租户知识库文档原文为权威（按行号 L<no> 对齐），"
         "同一行号只计一次；块尾残行不计入计数并单独列出；"
         "子串条件为行内精确匹配（大小写敏感），级别条件为行内级别字段精确匹配；"
         "时间范围取范围内完整行的第5列精确时间戳的最小/最大值。")


def _split_bgl_line(line: str) -> tuple[str, list[str], str] | None:
    """拆 "L<no>: ..." 行为 (行号, 字段列表, 消息部分)。非该格式返回 None。"""
    m = re.match(r"^\s*L(\d{3,8}):\s*(.*)$", line)
    if not m:
        return None
    fields = m.group(2).split()
    return m.group(1), fields, m.group(2)


def parse_conditions(question: str) -> tuple[list[dict], list[str], dict]:
    """保守解析问题中的统计条件。返回 (conditions, unsupported, wants)。"""
    conditions: list[dict] = []
    unsupported: list[str] = []
    wants = {"count": bool(_COUNT_CUE.search(question)),
             "time": bool(_TIME_CUE.search(question)),
             "mode": bool(_MODE_CUE.search(question))}
    # 级别条件：问题中出现的级别词（词边界，避免把普通英文单词当级别）
    for w in _LEVEL_WORDS:
        if re.search(rf"(?<![A-Za-z]){w}(?![A-Za-z])", question):
            conditions.append({"kind": "level", "value": w})
    # 子串条件：问题中的 ASCII 词序列（≥2 个词，长度≥8），排除已被当作级别的单独词
    seen = set()
    for m in _ASCII_SEQ.finditer(question):
        s = m.group(0).strip()
        if len(s) < 8 or s in seen:
            continue
        if any(re.search(rf"(?<![A-Za-z]){w}(?![A-Za-z])", s) and len(s.split()) == 1 for w in _LEVEL_WORDS):
            continue
        seen.add(s)
        conditions.append({"kind": "substring", "value": s})
    if wants["count"] and not conditions:
        unsupported.append("计数对象未能确定性解析：请说明要统计的具体消息内容或日志级别")
    if wants["mode"]:
        unsupported.append("“出现最多”类统计需要先约定消息归一化口径（如何处理变化字段），当前不支持，请澄清")
    return conditions, unsupported, wants


def compute_range_stats(question: str, hits: list[dict], db=None, tenant_id: str = "",
                        docs: list[tuple[str, str]] | None = None) -> dict | None:
    """对问题中的显式行范围做确定性统计。无显式行范围时返回 None。

    行内容解析与证据组装同源（租户文档为权威源，完整行优先，残行不计入）。
    """
    targets = parse_line_targets(question)
    if not targets:
        return None
    if docs is None:
        docs = tenant_docs(db, tenant_id) if (db is not None and tenant_id) else []
    found, incomplete = _resolve_target_lines(targets, hits, docs)
    resolved = [t for t in targets if t in found]
    missing = [t for t in targets if t not in found]
    conditions_spec, unsupported, wants = parse_conditions(question)

    lines = []  # (lineno, line_text) 仅完整行；残行不进入任何计数，单独报告
    for t in resolved:
        if t in incomplete:
            continue
        _title, line = found[t]
        lines.append((t, line))
    incomplete_now = [t for t in resolved if t in incomplete]

    total_lines = len(lines)
    conditions = []
    for spec in conditions_spec:
        matched: list[int] = []
        for lineno, line in lines:
            parsed = _split_bgl_line(line)
            if spec["kind"] == "level":
                if parsed and len(parsed[1]) > 7 and parsed[1][7] == spec["value"]:
                    matched.append(lineno)
            else:  # substring：行内精确匹配（含行号前缀后的全部内容）
                if spec["value"] in line:
                    matched.append(lineno)
        conditions.append({**spec, "count": len(matched), "line_numbers": matched})

    time_range = None
    if wants["time"] or True:
        # 时间范围始终随统计给出（范围内首末完整行的第5列时间戳），供解释引用
        stamps = []
        for lineno, line in lines:
            parsed = _split_bgl_line(line)
            if parsed and len(parsed[1]) > 3 and _TIMESTAMP.fullmatch(parsed[1][3] or ""):
                stamps.append((lineno, parsed[1][3]))
        if stamps:
            first = min(stamps, key=lambda x: x[1])
            last = max(stamps, key=lambda x: x[1])
            time_range = {"field": "第5列精确时间戳", "first_line": first[0], "first": first[1],
                          "last_line": last[0], "last": last[1]}

    return {
        "ranges": [{"lo": min(targets), "hi": max(targets), "requested": len(targets),
                    "resolved": len(resolved), "missing": missing}],
        "conditions": conditions,
        "total_lines": total_lines,
        "incomplete_excluded": incomplete_now,
        "missing_lines": missing,
        "time_range": time_range,
        "unsupported": unsupported,
        "basis": BASIS,
    }

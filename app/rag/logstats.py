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
_MODE_CUE = re.compile(r"出现最多|最常见|最多的消息|哪种消息最多|哪种最多|分布|频次|分组|哪几种|几种不同|有哪几种")
# 要求归一化/模板口径但未给出规则的信号：字面分组不能替用户发明归一化规则
_TEMPLATE_CUE = re.compile(r"归一化|归一后|模板|形式|模式分类|去重规则")
_ASCII_SEQ = re.compile(r"[A-Za-z][A-Za-z0-9_./-]*(?:\s+[A-Za-z][A-Za-z0-9_./-]*)+")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}-\d{2}\.\d{2}\.\d{2}\.\d{6}")

# 剥离字段定义（分组口径）：消息原文 = 去掉行号前缀 "L<no>:" 后，按空白切分的前 8 个字段
# （epoch、日期、节点、精确时间戳、节点2、RAS、KIND、LEVEL）之后的全部剩余文本，
# 以单个空格重新连接。分组键为该消息原文的逐字精确匹配（含大小写、空格与标点），
# 不做任何归一化/去数字/模板折叠——不同原文一律视为不同消息，绝不合并语义。
STRIPPED_FIELD_COUNT = 8
LITERAL_GROUP_BASIS = ("分组口径：按消息正文逐字精确分组。剥离字段为行号前缀与前 8 个空白分隔字段"
                       "（epoch、日期、节点、精确时间戳、节点2、RAS、KIND、LEVEL），"
                       "分组键为第 9 个字段起的原文剩余部分——保留正文内部原始空格与标点，"
                       "不 rejoin、不折叠、不做归一化；不同原文（含空格差异）一律视为不同消息。")

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


def _message_literal(line: str) -> str | None:
    """取消息正文逐字原文：剥离行号前缀与前 8 个空白分隔字段，保留其后全部字符
    （含正文字段间的原始空格——不 rejoin、不折叠，终审 P1-Q-LITERAL）。
    字段间分隔空白在本函数内跳过；返回值以前 9 个字段的第 9 字段（消息）首字符起，
    至行尾（含行内尾随空格，不含行终止符）。非该格式返回 None。"""
    m = re.match(r"^\s*L\d{3,8}:", line)
    if not m:
        return None
    rest = line[m.end():]
    pos, count = 0, 0
    while count < STRIPPED_FIELD_COUNT:
        while pos < len(rest) and rest[pos] in " \t":
            pos += 1
        if pos >= len(rest):
            return ""
        start = pos
        while pos < len(rest) and rest[pos] not in " \t":
            pos += 1
        count += 1
        if count == STRIPPED_FIELD_COUNT:
            return rest[pos:].lstrip(" \t")
    return ""


def parse_conditions(question: str) -> tuple[list[dict], list[str], dict]:
    """保守解析问题中的统计条件。返回 (conditions, unsupported, wants)。"""
    conditions: list[dict] = []
    unsupported: list[str] = []
    wants = {"count": bool(_COUNT_CUE.search(question)),
             "time": bool(_TIME_CUE.search(question)),
             "mode": bool(_MODE_CUE.search(question)),
             "template": bool(_TEMPLATE_CUE.search(question))}
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
        if wants["template"]:
            # 归一化/模板口径未指定规则：字面分组不能替用户发明归一化规则
            unsupported.append("问题要求归一化/模板形式的分布，但未指定归一化规则；"
                               "可先按消息正文逐字精确分组查看（literal_groups），"
                               "归一化口径需用户澄清后才能作为确定性结果")
        else:
            # 无归一化要求：按消息正文逐字精确分组给出频次与最多项（并列全部列出）
            conditions.append({"kind": "literal_top", "value": "按消息正文逐字精确分组"})
    # 前缀条件：问题明示"以 X 开头/前缀" → 消息正文 startswith 精确匹配（确定性口径）；
    # 计数为 0 时模板会提示核对前缀写法（如冒号/标点差异）并列出实际消息原文，不猜口径。
    for m in re.finditer(r"(?:以|以「|按)?[「」]?\s*([A-Za-z][\w :/\\.-]{2,60}?)\s*[」]?\s*(?:开头|为前缀|作为前缀)", question):
        value = m.group(1).strip()
        if value and not any(c["kind"] == "prefix" and c["value"] == value for c in conditions):
            conditions.append({"kind": "prefix", "value": value})
    return conditions, unsupported, wants


def group_messages_literal(lines: list[tuple[int, str]]) -> list[dict]:
    """按消息正文逐字精确分组：返回 [{message, count, line_numbers}]，按次数降序、原文次序稳定。

    分组键为 _message_literal 的逐字原文（保留正文内部原始空格，不 rejoin、不折叠，
    终审 P1-Q-LITERAL："A  B" 与 "A B" 是两个不同分组）；不同原文一律不合并。仅接收完整行。
    """
    groups: dict[str, list[int]] = {}
    for lineno, line in lines:
        message = _message_literal(line)
        if message is None:
            continue
        groups.setdefault(message, []).append(lineno)
    items = [{"message": msg, "count": len(nums), "line_numbers": nums} for msg, nums in groups.items()]
    items.sort(key=lambda x: (-x["count"], x["message"]))
    return items


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
            message = _message_literal(line) or ""
            if spec["kind"] == "level":
                if parsed and len(parsed[1]) > 7 and parsed[1][7] == spec["value"]:
                    matched.append(lineno)
            elif spec["kind"] == "prefix":
                if message.startswith(spec["value"]):
                    matched.append(lineno)
            else:  # substring：行内精确匹配（含行号前缀后的全部内容）
                if spec["value"] in line:
                    matched.append(lineno)
        entry = {**spec, "count": len(matched), "line_numbers": matched}
        if spec["kind"] == "prefix" and len(matched) == 0 and lines:
            # 前缀精确匹配为 0 时的确定性提示：列出范围内实际出现的不同消息原文，
            # 供用户核对前缀写法差异（如冒号、括号）；不做模糊匹配、不猜口径。
            contains = group_messages_literal(lines)
            hits = [g for g in contains if spec["value"] in g["message"]]
            distinct_note = ("范围内实际出现的不同消息原文共 " + str(len(contains)) + " 种：" +
                             "；".join(f"「{g['message'][:160]}」×{g['count']}" for g in contains[:5]))
            entry["note"] = ("以「" + spec["value"] + "」为前缀的逐字精确匹配为 0 条；"
                             "请核对前缀写法（如冒号、括号等标点差异）或澄清口径。" + distinct_note)
            if hits:
                entry["suggested_contains"] = {
                    "note": "以下为【包含】该前缀文本的消息（包含口径，非前缀口径），仅用于核对写法差异",
                    "items": [{"message": g["message"], "count": g["count"]} for g in hits[:5]]}
        conditions.append(entry)

    # 按消息正文逐字精确分组（仅当问题请求频次/最多项且未要求未指定的归一化口径）
    literal_groups = None
    top_cond = next((c for c in conditions if c["kind"] == "literal_top"), None)
    if top_cond is not None:
        groups = group_messages_literal(lines)
        top_count = groups[0]["count"] if groups else 0
        tops = [g for g in groups if g["count"] == top_count] if groups else []
        top_cond["top_count"] = top_count
        top_cond["top_items"] = [{"message": g["message"], "count": g["count"]} for g in tops]
        top_cond["line_numbers"] = [n for g in tops for n in g["line_numbers"]]
        literal_groups = {"basis": LITERAL_GROUP_BASIS, "distinct_messages": len(groups),
                          "groups": groups[:20], "listing_truncated": len(groups) > 20}

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

    # 节点/位置字段摘要（无条件给出）：字段2为节点/位置原文，逐字计数
    nodes_summary = None
    node_counter: dict[str, list[int]] = {}
    for lineno, line in lines:
        parsed = _split_bgl_line(line)
        if parsed and len(parsed[1]) > 2:
            node_counter.setdefault(parsed[1][2], []).append(lineno)
    if node_counter:
        nodes_summary = [{"node": n, "count": len(nums), "line_numbers": nums}
                         for n, nums in sorted(node_counter.items(), key=lambda x: (-len(x[1]), x[0]))]

    return {
        "ranges": [{"lo": min(targets), "hi": max(targets), "requested": len(targets),
                    "resolved": len(resolved), "missing": missing}],
        "conditions": conditions,
        "total_lines": total_lines,
        "incomplete_excluded": incomplete_now,
        "missing_lines": missing,
        "time_range": time_range,
        "nodes": nodes_summary,
        "literal_groups": literal_groups,
        "unsupported": unsupported,
        "basis": BASIS,
    }

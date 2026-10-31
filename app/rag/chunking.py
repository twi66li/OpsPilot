"""文本分块：先按 Markdown 标题切分；超长段落按完整行分组，无行界的超长单行回退字符窗口。

分块永不把带行界的文本从行中间切断（终审 P1-EVIDENCE 整改）：日志行被切半会让模型看到
"行号只剩前几位"的残行并导致计数/行号类回答出错。行分组模式下相邻块不重复
（原 overlap 滑窗会使同一行出现在两个块中，是计数类问题的重复计数隐患）；
无换行的纯散文段落无行界可依，保留原字符滑窗行为。
"""


def split_into_chunks(text: str, chunk_size: int = 500, overlap: int = 80) -> list[str]:
    text = text.strip()
    if not text:
        return []
    root_title = next((line for line in text.splitlines() if line.startswith("# ")), "")
    sections: list[str] = []
    current: list[str] = []

    for line in text.splitlines():
        has_body = any(part.strip() and not part.startswith("#") for part in current)
        if line.startswith("#") and current and has_body:
            sections.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current))

    chunks: list[str] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if root_title and not section.startswith(root_title):
            section = root_title + "\n" + section
        if len(section) <= chunk_size:
            chunks.append(section)
            continue
        # 完整行分组；单行超过 chunk_size（无行界的散文段落）回退字符滑窗
        current_lines: list[str] = []
        current_len = 0
        for line in section.splitlines(keepends=True):
            if len(line) > chunk_size:
                if current_lines:
                    chunks.append("".join(current_lines).strip())
                    current_lines, current_len = [], 0
                step = max(chunk_size - overlap, 1)
                for start in range(0, len(line), step):
                    piece = line[start : start + chunk_size].strip()
                    if piece:
                        chunks.append(piece)
                continue
            if current_lines and current_len + len(line) > chunk_size:
                chunks.append("".join(current_lines).strip())
                current_lines, current_len = [], 0
            current_lines.append(line)
            current_len += len(line)
        if current_lines:
            chunks.append("".join(current_lines).strip())
    return chunks

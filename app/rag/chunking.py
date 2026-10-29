"""文本分块：先按 Markdown 标题切分，再对超长段落滑动窗口。"""


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
        step = max(chunk_size - overlap, 1)
        for start in range(0, len(section), step):
            piece = section[start : start + chunk_size].strip()
            if piece:
                chunks.append(piece)
    return chunks

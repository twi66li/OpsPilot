"""轻量文本 PDF、Markdown 与离线 MinerU content_list 的统一解析入口。"""
import json
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


class _TableText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self.cell = []

    def handle_data(self, data):
        self.cell.append(data.strip())

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.row.append(" ".join(x for x in self.cell if x))
        elif tag == "tr" and self.row:
            self.rows.append("| " + " | ".join(self.row) + " |")
            self.row = []


def markdown_blocks(text: str, page: int | None = None) -> list[dict]:
    blocks, lines, headings = [], [], []
    fence = ""

    def flush():
        content = "\n".join(lines).strip()
        if content:
            kind = "code" if content.startswith(("```", "~~~")) else "table" if content.startswith("|") else "text"
            blocks.append({"text": content, "kind": kind, "heading_path": list(headings),
                           "page_start": page, "page_end": page})
        lines.clear()

    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if not fence:
                flush()
                fence = marker[1][0]
            elif marker[1][0] == fence:
                lines.append(line)
                flush()
                fence = ""
                continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line) if not fence else None
        if heading:
            flush()
            headings = headings[:len(heading[1]) - 1] + [heading[2].strip()]
        elif not line.strip() and not fence:
            flush()
        else:
            lines.append(line)
    flush()
    return blocks


def parse_file(original_path: str, filename: str, parsed_path: str = "") -> tuple[list, list, str]:
    suffix = Path(filename).suffix.lower()
    warnings = []
    if parsed_path:
        from pypdf import PdfReader

        page_count = len(PdfReader(original_path).pages)
        items = json.loads(Path(parsed_path).read_text(encoding="utf-8-sig"))
        if not isinstance(items, list) or not items:
            raise ValueError("MinerU 解析文件必须是非空 content_list JSON 数组")
        pages_by_number = {}
        for item in items:
            if not isinstance(item, dict) or type(item.get("page_idx")) is not int or item["page_idx"] < 0:
                raise ValueError("每个 MinerU 内容项必须提供从0开始的 page_idx")
            page = item["page_idx"] + 1
            if page > page_count:
                raise ValueError("MinerU页码超出原PDF页数，请确认解析文件与原文件匹配")
            kind = item.get("type")
            if kind == "text":
                text = str(item.get("text", ""))
                if item.get("text_level"):
                    text = "#" * min(int(item["text_level"]), 6) + " " + text
            elif kind == "table":
                table = _TableText()
                table.feed(str(item.get("table_body", "")))
                rows = table.rows
                if len(rows) > 1:
                    rows.insert(1, "| " + " | ".join(["---"] * (rows[0].count("|") - 1)) + " |")
                text = "\n\n".join(["\n".join(item.get("table_caption", [])), "\n".join(rows),
                                     "\n".join(item.get("table_footnote", []))]).strip()
            elif kind == "code":
                text = "```" + str(item.get("code_language", "")) + "\n" + str(item.get("code_body", "")) + "\n```"
            else:
                warnings.append(f"第{page}页的{kind}内容未作为文本索引")
                continue
            pages_by_number.setdefault(page, []).append(text)
        pages = [{"page": n, "text": "\n\n".join(texts)} for n, texts in sorted(pages_by_number.items())]
        parser = "mineru-content-list"
    elif suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(original_path)
        if reader.is_encrypted:
            raise ValueError("暂不支持加密PDF，请先解密")
        if len(reader.pages) > 100:
            raise ValueError("演示环境最多解析100页，请在本机解析后提交MinerU结果")
        pages = []
        for number, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()
            pages.append({"page": number, "text": text})
            if not text:
                warnings.append(f"第{number}页没有可提取文本，可能需要OCR")
        # 只移除跨至少三页重复的首尾行，不修改正文中的重复操作步骤。
        edges = Counter(line for p in pages for line in set(p["text"].splitlines()[:1] + p["text"].splitlines()[-1:]))
        repeated = {line for line, count in edges.items() if count >= max(3, len(pages) * 0.6)}
        for p in pages:
            lines = p["text"].splitlines()
            if lines and lines[0] in repeated:
                lines.pop(0)
            if lines and lines[-1] in repeated:
                lines.pop()
            p["text"] = "\n".join(lines)
        parser = "pypdf"
        warnings.append("文本PDF按物理页序提取，不保证恢复复杂表格和多栏版面；复杂文档请导入MinerU结果")
    else:
        pages = [{"page": None, "text": Path(original_path).read_text(encoding="utf-8-sig")}]
        parser = "markdown" if suffix == ".md" else "text"
    if not any(p["text"].strip() for p in pages):
        raise ValueError("没有提取到正文；扫描PDF请先在本机用MinerU/OCR解析，再上传原PDF和content_list.json")
    if sum(len(p["text"]) for p in pages) > 500_000:
        raise ValueError("提取正文超过50万字符，请拆分文档")
    return pages, list(dict.fromkeys(warnings)), parser


def build_version_chunks(doc, version, pages: list[dict]) -> tuple[list[dict], list[dict]]:
    """章节形成有界父块；代码保持完整，大表子块重复表头，日志沿用完整行切分。"""
    from app.rag.chunking import split_into_chunks

    parents, chunks = [], []
    grouped = []
    for page in pages:
        blocks = []
        for block in markdown_blocks(page["text"], page["page"]):
            texts = split_into_chunks(block["text"], chunk_size=1800, overlap=0) if block["kind"] == "text" else [block["text"]]
            blocks.extend({**block, "text": text} for text in texts)
        for block in blocks:
            if len(block["text"]) > 8000 and block["kind"] == "code":
                raise ValueError("代码块超过8000字符，请按操作步骤拆分")
            if (grouped and grouped[-1]["heading_path"] == block["heading_path"]
                    and grouped[-1]["page_start"] == block["page_start"]
                    and len(grouped[-1]["text"]) + len(block["text"]) < 1800):
                grouped[-1]["text"] += "\n\n" + block["text"]
                grouped[-1]["blocks"].append(block)
            else:
                grouped.append({**block, "blocks": [block]})
    for i, group in enumerate(grouped):
        parent_id = f"{version.id}:p{i}"
        location = {k: group[k] for k in ("heading_path", "page_start", "page_end")}
        heading = " / ".join(group["heading_path"])
        parents.append({"id": parent_id, "text": (heading + "\n\n" + group["text"]).strip(), "location": location})
        for block in group["blocks"]:
            if block["kind"] == "code":
                pieces = [block["text"]]
            elif block["kind"] == "table":
                lines = block["text"].splitlines()
                header = "\n".join(lines[:2])
                pieces = [header + "\n" + part for part in split_into_chunks("\n".join(lines[2:]), overlap=0)] or [header]
            else:
                pieces = split_into_chunks(block["text"], overlap=0)
            for text in pieces:
                chunks.append({"chunk_id": f"{version.id}:c{len(chunks)}", "doc_id": doc.id,
                               "doc_title": doc.title, "source": doc.source, "source_name": version.filename,
                               "text": (heading + "\n" + text).strip(), "chunk_index": len(chunks),
                               "version_id": version.id, "version": version.number, "parent_id": parent_id,
                               "content_type": block["kind"], "parser": version.parser, **location})
    return parents, chunks

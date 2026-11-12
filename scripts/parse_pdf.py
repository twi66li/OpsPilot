"""本机PDF解析；优先使用已安装的MinerU，普通文本PDF可使用现有pypdf。"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from pypdf import PdfReader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/local-pdf"))
    parser.add_argument("--engine", choices=("auto", "mineru", "pypdf"), default="auto")
    args = parser.parse_args()
    pdf = args.pdf.resolve()
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        parser.error("请选择存在的PDF文件")
    output = args.output.resolve() / pdf.stem
    output.mkdir(parents=True, exist_ok=True)
    mineru = shutil.which("mineru")
    if args.engine == "mineru" or args.engine == "auto" and mineru:
        if not mineru:
            parser.error("未找到MinerU，请先按https://github.com/opendatalab/MinerU安装本机解析工具")
        help_text = subprocess.run([mineru, "--help"], capture_output=True, text=True, check=True).stdout
        if "--path" not in help_text or "--method" not in help_text:
            parser.error(
                "当前导入工具支持MinerU 2/3的content_list格式；新版请导出兼容Content List V1，或用 --engine pypdf 提取普通文本"
            )
        subprocess.run([mineru, "-p", str(pdf), "-o", str(output), "-m", "auto"], check=True)
        results = list(output.rglob("*_content_list.json"))
        if not results:
            raise RuntimeError("解析完成但未找到content_list.json，请检查MinerU输出")
        print("请同时上传原PDF及解析文件：", results[0])
    else:
        reader = PdfReader(str(pdf))
        content = [
            {"type": "text", "text": page.extract_text() or "", "page_idx": i} for i, page in enumerate(reader.pages)
        ]
        if not any(block["text"].strip() for block in content):
            parser.error("没有可提取文本；扫描PDF需要安装MinerU后使用 --engine mineru")
        target = output / "content_list.json"
        target.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已提取文本（未执行OCR，不保证复杂表格布局）：", target)


if __name__ == "__main__":
    main()

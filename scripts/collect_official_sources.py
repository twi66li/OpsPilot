"""手动刷新固定官方页面的本地核对快照；不自动替换已经核对的知识库内容。"""
import hashlib
import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

from app.services.official_library import CATALOG


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def collect():
    folder = Path("data/official_sources")
    folder.mkdir(parents=True, exist_ok=True)
    failed = []
    urls = list(dict.fromkeys(url.split("#")[0] for item in CATALOG
                             for url in [item["url"], *item.get("additional_urls", [])]))
    for url in urls:
        try:
            with urlopen(Request(url, headers={"User-Agent": "OpsPilot-source-check/1.0"}), timeout=30) as response:
                raw = response.read(4 * 1024 * 1024)
                final_url = response.url
            parser = PageText()
            parser.feed(raw.decode("utf-8"))
            text = "\n".join(parser.parts)
            if len(text) < 200:
                raise ValueError("页面正文不足，请手动核对")
            record = {"url": url, "final_url": final_url, "fetched_at": datetime.now(timezone.utc).isoformat(),
                      "sha256": hashlib.sha256(raw).hexdigest(), "text": text,
                      "review_status": "待人工核对；下载成功不代表知识库已更新"}
            path = folder / (hashlib.sha256(url.encode()).hexdigest()[:16] + ".json")
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已采集 {url} ({len(text)} 字符)")
        except Exception as exc:
            failed.append(url)
            print(f"采集失败 {url}: {type(exc).__name__}")
    return failed


if __name__ == "__main__":
    raise SystemExit(bool(collect()))

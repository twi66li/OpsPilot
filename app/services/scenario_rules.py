"""三个现有场景的唯一配置入口；匹配结果只是日志线索，不是根因或恢复结论。"""

import json
import re
from pathlib import Path

SCENARIOS = json.loads((Path(__file__).resolve().parents[1] / "knowledge" / "scenarios.json").read_text(encoding="utf-8"))
BY_COLLECTION_KEY = {item["collection_key"]: item for item in SCENARIOS}


def match_scenarios(text: str) -> list[dict]:
    lines = text.splitlines()
    matches = []
    for scenario in SCENARIOS:
        signals = []
        for signal in scenario["signals"]:
            pattern = re.compile(signal["pattern"], re.I)
            found = [number for number, line in enumerate(lines, 1) if pattern.search(line)]
            if found:
                signals.append({"label": signal["label"], "count": len(found), "lines": found})
        if signals:
            matches.append({"id": scenario["id"], "title": scenario["title"], "doc_id": scenario["doc_id"],
                            "collection_key": scenario["collection_key"], "signals": signals})
    return matches

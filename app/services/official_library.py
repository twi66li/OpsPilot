"""固定官方来源的整理语料；共享只读，与租户私有文档及演练语料分开。"""
import hashlib
import json
from pathlib import Path

from app.db.models import Document, Tenant
from app.services.kb_service import ingest_document

OFFICIAL_TENANT = "__opspilot_official__"
CATALOG = json.loads((Path(__file__).resolve().parents[1] / "knowledge" / "official.json").read_text(encoding="utf-8"))
SCENARIOS = json.loads((Path(__file__).resolve().parents[1] / "knowledge" / "scenarios.json").read_text(encoding="utf-8"))
for guide in SCENARIOS:
    item = next(item for item in CATALOG if "official-" + item["id"] == guide["doc_id"])
    sections = [f"\n\n## 场景指南：{guide['title']}", guide["description"], "常见现象：" + "；".join(guide["symptoms"])]
    for step in guide["checks"]:
        sections.extend([f"### {step['where']}（{step['risk']}）", step["action"], "正常：" + step["normal"], "异常：" + step["abnormal"]])
    sections.extend(["修改与回退：" + guide["change_guidance"], "恢复核对：" + guide["verification"],
                     "以下仅为教学样例，不是用户网站的运行证据。", "处理前样例：\n" + guide["sample_before"], "处理后样例：\n" + guide["sample_after"]])
    item["content"] += "\n".join(sections)
    item["scenario_id"] = guide["id"]
    item["checked_at"] = "2026-11-12"
BY_ID = {"official-" + item["id"]: item for item in CATALOG}


def ensure_library(db, container):
    if not db.get(Tenant, OFFICIAL_TENANT):
        db.add(Tenant(id=OFFICIAL_TENANT, name="平台官方来源知识库"))
        db.commit()
    for doc_id, item in BY_ID.items():
        content = (f"# {item['title']}\n\n适用版本：{item['applicable_version']}\n"
                   f"核对日期：{item['checked_at']}\n资料性质：OpsPilot依据官方资料整理，不是官方原文。\n"
                   f"官方出处：{item['url']}\n\n{item['content']}")
        previous = db.get(Document, doc_id)
        if previous and previous.content == content:
            continue
        if previous:
            container.vector_store.delete_doc(OFFICIAL_TENANT, doc_id)
            container.bm25.remove_doc(OFFICIAL_TENANT, doc_id)
            previous.content = content
        doc = previous or Document(id=doc_id, tenant_id=OFFICIAL_TENANT, title=item["title"],
                                   source=item["url"], content=content, created_by="platform")
        ingest_document(db, container, doc)


def catalog():
    return [{"doc_id": doc_id, **item,
             "content_hash": hashlib.sha256(item["content"].encode()).hexdigest(),
             "source_kind": "official", "source_label": "平台整理 · 官方资料来源"}
            for doc_id, item in BY_ID.items()]

"""固定官方来源的整理语料；共享只读，与租户私有文档及演练语料分开。"""
import hashlib
import json
from pathlib import Path

from app.db.models import Document, Tenant
from app.services.kb_service import ingest_document

OFFICIAL_TENANT = "__opspilot_official__"
CATALOG = json.loads((Path(__file__).resolve().parents[1] / "knowledge" / "official.json").read_text(encoding="utf-8"))
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

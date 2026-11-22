"""个人排障资料混合检索与只读查询工具辅助函数。"""

import hashlib
import json

from app.db.models import ChatSession, Document, SupportCase, WebsiteProfile
from app.rag.embeddings import tokenize
from app.rag.fusion import rrf_fuse
from app.services.case_service import case_status, is_archived
from app.services.knowledge_access import personal_namespace
from app.services.official_library import BY_ID, OFFICIAL_TENANT


def retrieve_for_case(container, tenant_id, user_id, context, query, top_k):
    """先过滤授权文档和有效版本，再召回；两个知识域共享一次query embedding及重排。"""
    with container.db_session() as db:
        private = (
            db.query(Document)
            .filter(
                Document.tenant_id.in_((tenant_id, personal_namespace(user_id))),
                Document.created_by == user_id,
                Document.deleted_at.is_(None),
                Document.id.in_(context.get("profile", {}).get("doc_ids", [])),
            )
            .all()
        )
        official = (
            []
            if context.get("exclude_official")
            else db.query(Document).filter_by(tenant_id=OFFICIAL_TENANT, deleted_at=None).all()
        )
        vector = container.embedder.embed([query])[0]
        routes = []
        for scope, docs in (
            (OFFICIAL_TENANT, official),
            (tenant_id, [d for d in private if d.tenant_id == tenant_id]),
            (personal_namespace(user_id), [d for d in private if d.tenant_id == personal_namespace(user_id)]),
        ):
            if not docs:
                continue
            active = {d.id: d.active_version_id for d in docs}
            routes.append(container.vector_store.search(scope, vector, top_k=8, active_versions=active))
            routes.append(container.bm25.search(scope, query, top_k=8, active_versions=active))
    candidate_k = max(top_k * 3, 15) if container.reranker else max(top_k, 8)
    candidates = rrf_fuse(routes, k=60, top_k=candidate_k)
    hits = container.reranker.rerank(query, candidates, top_k=top_k) if container.reranker else candidates[:top_k]
    output = []
    for hit in hits:
        item = hit.to_dict()
        if hit.doc_id in BY_ID:
            source = BY_ID[hit.doc_id]
            item.update(
                source_kind="official", applicable_version=source["applicable_version"], checked_at=source["checked_at"]
            )
            item["parent_text"] = source["content"]
            item["content_hash"] = hashlib.sha256(source["content"].encode()).hexdigest()
        else:
            item["source_kind"] = "private"
        output.append(item)
    return output


def read_evidence_lines(evidence, visible, evidence_id, line_start, line_end):
    item = evidence.get(evidence_id)
    lines = item["text"].splitlines() if item else []
    if not 1 <= line_start <= line_end <= len(lines) or line_end - line_start >= 12:
        return {"status": "no_data", "summary": "请指定本次证据中连续且实际存在的1至12行"}
    visible[evidence_id].update(range(line_start, line_end + 1))
    return {
        "status": "ok",
        "evidence_id": item["id"],
        "summary": f"已读取原文 L{line_start}–L{line_end}",
        "lines": [{"line": number, "text": lines[number - 1]} for number in range(line_start, line_end + 1)],
    }


def get_site_overview(container, state, context):
    with container.db_session() as db:
        site = (
            db.query(WebsiteProfile)
            .filter_by(
                id=context.get("site_id"),
                tenant_id=state["tenant_id"],
                user_id=state["user_id"],
                deleted_at=None,
            )
            .first()
        )
        rows = (
            db.query(SupportCase)
            .join(ChatSession)
            .filter(
                SupportCase.site_id == context.get("site_id"),
                SupportCase.tenant_id == state["tenant_id"],
                SupportCase.user_id == state["user_id"],
                ChatSession.deleted_at.is_(None),
            )
            .all()
        )
        return {
            "status": "ok" if site else "no_data",
            "profile": site.data if site else {},
            "session_profile_snapshot": context.get("profile", {}),
            "problem_count": sum(r.data.get("kind", "problem") == "problem" for r in rows) if site else 0,
            "summary": "读取已保存的网站信息；不包含网站实时运行状态" if site else "尚未关联网站，可继续基础咨询",
        }


def search_saved_cases(container, state, context, query, top_k):
    with container.db_session() as db:
        rows = (
            db.query(SupportCase)
            .join(ChatSession)
            .filter(
                SupportCase.tenant_id == state["tenant_id"],
                SupportCase.user_id == state["user_id"],
                SupportCase.site_id == context.get("site_id"),
                ChatSession.deleted_at.is_(None),
            )
            .all()
        )
    terms = [token for token in tokenize(query) if len(token) > 1]
    matches = [
        r
        for r in rows
        if (is_archived(r) or case_status(r) == "user_resolved")
        and r.data.get("kind", "problem") == "problem"
        and r.session_id != state["session_id"]
        and any(term in json.dumps(r.data, ensure_ascii=False).lower() for term in terms)
    ]
    return {
        "status": "ok" if matches else "no_data",
        "summary": "仅返回本人在同一网站确认保存的案例；不能证明本次根因",
        "cases": [
            {
                "session_id": r.session_id,
                "solution": r.data.get("solution"),
                "previous_report": r.data.get("report"),
                "profile": r.data.get("profile"),
                "outcome": case_status(r),
                "archived": is_archived(r),
            }
            for r in matches[:top_k]
        ],
    }

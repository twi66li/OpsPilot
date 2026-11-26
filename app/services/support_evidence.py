"""排障输入模型、敏感信息脱敏、资源归属权校验、证据构造与会话上下文。"""

import ipaddress
import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from app.db.models import ChatSession, Document, SupportCase, WebsiteProfile
from app.services.knowledge_access import personal_namespace
from app.services.maintenance_timeline import recent_site_changes


class DependencyIn(BaseModel):
    source: str = Field(min_length=1, max_length=80)
    target: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def meaningful_relation(self):
        self.source, self.target = self.source.strip(), self.target.strip()
        if not self.source or not self.target or self.source == self.target:
            raise ValueError("依赖关系需要两个不同的组件名称")
        return self


class SiteIn(BaseModel):
    name: str = Field(default="我的网站", min_length=1, max_length=80)
    url: str = Field(default="", max_length=1024)
    framework: str = Field(default="", max_length=120)
    deployment: str = Field(default="", max_length=120)
    database: str = Field(default="", max_length=120)
    versions: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=1200)
    doc_ids: list[str] = Field(default_factory=list, max_length=20)
    environment: Literal["unknown", "production", "staging", "test", "development"] = "unknown"
    group: str = Field(default="", max_length=80)
    owner: str = Field(default="", max_length=80)
    dependencies: list[DependencyIn] = Field(default_factory=list, max_length=12)


class CaseIn(BaseModel):
    title: str = Field(default="网站排障", min_length=1, max_length=100)
    site_id: str | None = None
    kind: Literal["problem", "consult", "history"] = "problem"
    draft: bool = False
    symptom: str = Field(default="", max_length=2000)
    endpoint: str = Field(default="", max_length=200)
    impact: str = Field(default="", max_length=500)
    started_at: str = Field(default="", max_length=100)
    recent_changes: str = Field(default="", max_length=1000)


class EvidenceIn(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    label: str = Field(default="日志片段", min_length=1, max_length=100)
    kind: Literal["initial", "check_result"] = "initial"
    collected_at: str = Field(default="", max_length=80)
    endpoint: str = Field(default="", max_length=200)
    plan_revision: int | None = None
    step_index: int | None = Field(default=None, ge=0, le=2)


_SECRET = r"(?:password|passwd|pwd|access[_-]?token|refresh[_-]?token|access[_-]?key[_-]?secret|client[_-]?secret|token|api[_-]?key|secret|cookie|authorization)"
_REDACTORS = [
    (re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S), "[私钥已脱敏]", "私钥"),
    (re.compile(r"(?im)(authorization\s*:\s*)[^\r\n]+"), r"\1[已脱敏]", "认证头"),
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[已脱敏]", "Bearer令牌"),
    (re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^\s/:]+:)[^\s@]+(@)"), r"\1[已脱敏]\2", "连接串密码"),
    (re.compile(r"(?i)([\"\']?" + _SECRET + r"[\"\']?\s*[:=]\s*)([\"\'])(.*?)\2"), r"\1\2[已脱敏]\2", "口令或密钥"),
    (re.compile(r"(?i)(\b" + _SECRET + r"\s*[:=]\s*)(?!\[已脱敏\])[^\s,;&}\]\"']+"), r"\1[已脱敏]", "口令或密钥"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[邮箱已脱敏]", "邮箱"),
]


def redact(text):
    findings = {}
    for pattern, replacement, label in _REDACTORS:
        original = text

        def replace(match):
            value = match.expand(replacement)
            if label == "私钥":
                value += "\n" * match.group(0).count("\n")
            if value != match.group(0):
                finding = findings.setdefault(label, {"label": label, "count": 0, "lines": []})
                finding["count"] += 1
                finding["lines"].append(original.count("\n", 0, match.start()) + 1)
            return value

        text = pattern.sub(replace, text)
    review = {}
    for match in re.finditer(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", text):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        label = "私网或本机IPv4" if address.is_private or address.is_loopback else "IPv4地址"
        finding = review.setdefault(label, {"label": label, "count": 0, "lines": []})
        finding["count"] += 1
        finding["lines"].append(text.count("\n", 0, match.start()) + 1)
    return {
        "text": text,
        "replacements": sum(item["count"] for item in findings.values()),
        "findings": list(findings.values()),
        "review_findings": list(review.values()),
        "notice": "已遮蔽常见密钥、连接串密码和邮箱；IPv4保留用于排障关联并单独提示。请人工检查IPv6、自定义秘密字段及其他个人信息。",
    }


def owned_case(db, user, session_id):
    case = (
        db.query(SupportCase).filter_by(session_id=session_id, tenant_id=user.tenant_id, user_id=user.user_id).first()
    )
    session = db.get(ChatSession, session_id)
    if not case or not session or session.deleted_at is not None:
        raise HTTPException(404, "排障会话不存在")
    return case


def owned_site(db, user, site_id):
    site = (
        db.query(WebsiteProfile)
        .filter_by(id=site_id, tenant_id=user.tenant_id, user_id=user.user_id, deleted_at=None)
        .first()
    )
    if not site:
        raise HTTPException(404, "网站背景不存在")
    return site


def validate_private_docs(db, user, ids):
    docs = (
        db.query(Document)
        .filter(
            Document.id.in_(ids),
            Document.tenant_id == personal_namespace(user.user_id),
            Document.created_by == user.user_id,
            Document.deleted_at.is_(None),
        )
        .all()
    )
    if set(ids) != {d.id for d in docs}:
        raise HTTPException(400, "仅能关联本人上传的私人手册")


def context_for_session(db, session_id, tenant_id, user_id):
    case = db.query(SupportCase).filter_by(session_id=session_id, tenant_id=tenant_id, user_id=user_id).first()
    return (
        {
            "site_id": case.site_id,
            **case.data,
            "recent_changes": recent_site_changes(db, tenant_id, user_id, case.site_id),
        }
        if case
        else None
    )


def new_evidence(body):
    return {
        "id": "E" + uuid.uuid4().hex[:8],
        "label": redact(body.label)["text"],
        "kind": body.kind,
        "text": redact(body.text)["text"],
        "collected_at": redact(body.collected_at)["text"],
        "endpoint": redact(body.endpoint)["text"],
        "plan_revision": body.plan_revision,
        "step_index": body.step_index,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }

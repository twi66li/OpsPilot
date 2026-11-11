"""数据模型：租户/用户/会话/消息/文档元数据/故障记录/工单/审批单。

约定：所有业务表都带 tenant_id，查询层强制租户过滤；
向量数据（文档分块）存储在 Qdrant，payload 中同样携带 tenant_id。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # admin | engineer | viewer
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ChatSession(Base):
    """聊天会话（thread_id），LangGraph checkpointer 以此为 thread。"""

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(256), default="新会话")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), ForeignKey("chat_sessions.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Document(Base):
    """知识库文档（元数据与全文存 MySQL，分块向量存 Qdrant）。"""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    title: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(256), default="")
    content: Mapped[str] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    active_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class DocumentVersion(Base):
    """原文件与持久化入库任务；ready 与当前生效版本分开记录。"""

    __tablename__ = "document_versions"
    __table_args__ = (Index("uq_document_version_number", "doc_id", "number", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    doc_id: Mapped[str] = mapped_column(String(64), ForeignKey("documents.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(String(64))
    original_path: Mapped[str] = mapped_column(String(512))
    parsed_path: Mapped[str] = mapped_column(String(512), default="")
    parser: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="uploaded")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    pages: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    index_config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class DocumentParent(Base):
    __tablename__ = "document_parents"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    doc_id: Mapped[str] = mapped_column(String(64), index=True)
    version_id: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text)
    location: Mapped[dict] = mapped_column(JSON, default=dict)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    doc_id: Mapped[str] = mapped_column(String(64), index=True)
    version_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_id: Mapped[str] = mapped_column(String(96), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class FaultRecord(Base):
    """历史故障记录（Agent 的“故障史”工具查询对象）。"""

    __tablename__ = "fault_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    service: Mapped[str] = mapped_column(String(128), index=True)
    error_code: Mapped[str] = mapped_column(String(64), index=True, default="")
    title: Mapped[str] = mapped_column(String(256))
    symptom: Mapped[str] = mapped_column(Text, default="")
    root_cause: Mapped[str] = mapped_column(Text, default="")
    resolution: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(16), default="P2")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (Index("uq_tickets_approval_id", "approval_id", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(16), default="P2")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open|resolved|rejected
    created_by: Mapped[str] = mapped_column(String(64), default="")
    approver: Mapped[str] = mapped_column(String(64), default="")
    decision_comment: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApprovalRequest(Base):
    """LangGraph interrupt 产生的工单创建审批单（人工审批入口）。"""

    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    ticket_draft: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|approved|rejected
    requested_by: Mapped[str] = mapped_column(String(64), default="")
    decided_by: Mapped[str] = mapped_column(String(64), default="")
    comment: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RetrievalLog(Base):
    """检索日志：记录每次检索的模式/耗时/命中，供效果分析。"""

    __tablename__ = "retrieval_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    query: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(16))
    top_k: Mapped[int] = mapped_column(Integer, default=5)
    hit_doc_ids: Mapped[list] = mapped_column(JSON, default=list)
    scores: Mapped[list] = mapped_column(JSON, default=list)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="running")
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class RemediationRequest(Base):
    """演练修复审批与执行审计；一个调查、一个固定动作只执行一次。"""

    __tablename__ = "remediation_requests"
    __table_args__ = (Index("uq_remediation_run_action", "tenant_id", "run_id", "action", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64))
    service: Mapped[str] = mapped_column(String(64), default="order-service")
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    requested_by: Mapped[str] = mapped_column(String(64))
    decided_by: Mapped[str] = mapped_column(String(64), default="")
    approved: Mapped[bool | None] = mapped_column(nullable=True)
    comment: Mapped[str] = mapped_column(String(600), default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

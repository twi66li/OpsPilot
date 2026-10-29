"""LangGraph 诊断工作流的状态定义。"""
from typing import TypedDict


class DiagState(TypedDict, total=False):
    # 输入
    question: str
    session_id: str
    tenant_id: str
    user_id: str
    username: str
    history: list[dict]

    # 流程中间态
    intent: str  # diagnose | create_ticket
    rewritten_query: str
    hits: list[dict]
    docs_sufficient: bool
    iteration: int  # 检索轮次（grade 节点自增，控制补充检索次数）

    # 工具与证据
    tool_events: list[dict]
    fault_records: list[dict]
    metrics: dict

    # 输出
    answer: str
    citations: list[str]
    ticket_draft: dict | None
    approval: dict | None
    approval_id: str | None
    ticket_id: str | None

    # SSE 事件队列：节点把待推送事件写入此列表，服务层随流式更新转发
    events: list[dict]

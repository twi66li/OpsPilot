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
    run_id: str
    end_reason: str
    support_context: dict | None
    support_report: dict | None
    support_workflow: str
    # 证据路径策略（P1-QUALITY-20261102 必做A）：
    # range_stats_direct = 租户文档权威读取+程序统计（零 embedding/rerank/LLM）；
    # range_hybrid = 统计已程序化 + 混合检索补充语义上下文；hybrid = 常规混合检索。
    strategy: str

    # 流程中间态
    intent: str  # diagnose | create_ticket
    rewritten_query: str
    hits: list[dict]
    docs_sufficient: bool | None
    iteration: int  # 检索轮次（grade 节点自增，控制补充检索次数）

    # 工具与证据
    tool_events: list[dict]
    fault_records: list[dict]
    metrics: dict

    # 输出
    answer: str
    generation_policy: str  # llm | log_only_evidence_boundary（原始日志不外推根因/运行状态）
    citations: list[str]
    # [n]→第 n 条证据文档（与生成器编号同源，重复文档保留各自编号）；
    # 生成器实际收到的编号证据与范围组装元数据；必须声明在状态中，
    # 否则 LangGraph 会丢弃节点返回的未声明字段，最终响应拿不到映射（终审 P1-CITATIONS）。
    citations_indexed: list[str]
    used_evidence: list[dict]
    range_assembly: dict
    # 显式行范围的覆盖情况与确定性统计（P1-ARAG-20261102）：
    # 覆盖参与 grade 充足性决策；统计由程序计算，模型只做解释。
    range_coverage: dict
    range_stats: dict
    ticket_draft: dict | None
    approval: dict | None
    approval_id: str | None
    ticket_id: str | None

    # SSE 事件队列：节点把待推送事件写入此列表，服务层随流式更新转发
    events: list[dict]

"""LangGraph 诊断工作流的状态定义、字段归属与事件合并规则。

状态分层（保持顶层 DiagState 全部 34 个键名与类型 100% 向后兼容旧检查点）：
- RequestInputState：调用方传入的只读请求上下文（全图只读）
- RoutingState：主图路由与策略状态（route_intent / 分支入口写）
- RetrievalInputState / RetrievalState / RetrievalOutputState / RetrievalSubgraphState：
  检索-评估-改写闭环子图（retrieve -> grade -> rewrite）的显式输入/输出边界
- ToolEvidenceState：主图只读工具与采集证据状态（call_tools / native_tools 写）
- AnswerOutputState：主图回答、证据引用与范围统计输出（generate / pure_range_answer / native_tools / personal_support / create_ticket 写）
- ApprovalTicketState：主图人工审批与建单状态（generate / native_tools / prepare_approval / approval_gate / create_ticket 写）
- EventChannelState：主图单步事件通道（LastValue，供 agent_service._iter_graph_events 边到边消费）

事件推送与 reducer 规则：
1. 主图 DiagState.events 保持 LastValue（非累加）：每个节点仅返回本步新事件列表，
   agent_service._iter_graph_events 从 stream_mode="updates" 消费单步 delta["events"]，
   避免跨节点/跨轮重复推送与检查点膨胀。
2. 独立编译的检索子图 RetrievalSubgraphState.events 使用 merge_events reducer：
   在闭环单次运行内按执行顺序累加 retrieve/grade/rewrite 各步事件，避免后置节点覆盖前置事件。
"""

from typing import Annotated, TypedDict


def merge_events(existing: list[dict] | None, incoming: list[dict] | None) -> list[dict]:
    """子图内事件列表合并 reducer：顺序追加且不原地修改，避免事件覆盖或丢失。"""
    merged = list(existing or [])
    if incoming:
        merged.extend(item for item in incoming if item)
    return merged


class RequestInputState(TypedDict, total=False):
    """调用方（agent_service）注入的请求上下文；图内所有节点仅读取、不覆盖。"""

    question: str
    session_id: str
    tenant_id: str
    user_id: str
    username: str
    history: list[dict]
    run_id: str
    evaluation_time: float | None
    support_context: dict | None
    support_workflow: str


class RoutingState(TypedDict, total=False):
    """主图路由与策略控制状态。"""

    # 证据路径策略（P1-QUALITY-20261102 必做A）：
    # range_stats_direct = 租户文档权威读取+程序统计（零 embedding/rerank/LLM）；
    # range_hybrid = 统计已程序化 + 混合检索补充语义上下文；hybrid = 常规混合检索。
    strategy: str
    intent: str  # diagnose | create_ticket | chat | chat_retrieve
    end_reason: str


class RetrievalInputState(TypedDict, total=False):
    """检索-评估-改写闭环子图输入边界（只读透传字段 + 轮次初始态）。"""

    question: str
    tenant_id: str
    strategy: str
    rewritten_query: str
    iteration: int


class RetrievalState(TypedDict, total=False):
    """检索-评估-改写闭环子图拥有写权的业务状态字段。"""

    rewritten_query: str
    hits: list[dict]
    docs_sufficient: bool | None
    iteration: int  # 检索轮次（grade 节点自增，控制补充检索次数）
    # 显式行范围的覆盖情况（P1-ARAG-20261102）：参与 grade 充足性决策
    range_coverage: dict | None


class RetrievalOutputState(RetrievalState, total=False):
    """独立检索子图输出边界：仅回写检索闭环业务字段与合并后的闭环事件。"""

    events: Annotated[list[dict], merge_events]


class RetrievalSubgraphState(RetrievalInputState, RetrievalOutputState, total=False):
    """独立编译检索子图使用的完整状态（输入边界 + 输出边界 + merge_events reducer）。"""


class ToolEvidenceState(TypedDict, total=False):
    """主图诊断工具与采集证据状态。"""

    tool_events: list[dict]
    fault_records: list[dict]
    metrics: dict


class AnswerOutputState(TypedDict, total=False):
    """主图回答、引用映射与确定性统计输出状态。"""

    answer: str
    generation_policy: str | None  # llm | log_only_evidence_boundary（原始日志不外推根因/运行状态）
    citations: list[str]
    # [n]→第 n 条证据文档（与生成器编号同源，重复文档保留各自编号）；
    # 生成器实际收到的编号证据与范围组装元数据；必须声明在状态中，
    # LangGraph 只保留声明的字段，最终响应需要这些映射解析引用。
    citations_indexed: list[str]
    used_evidence: list[dict] | None
    range_assembly: dict | None
    # 显式行范围的确定性统计（P1-ARAG-20261102）：由程序计算，模型只做解释
    range_stats: dict | None
    support_report: dict | None


class ApprovalTicketState(TypedDict, total=False):
    """主图人工审批（interrupt）与建单状态。"""

    ticket_draft: dict | None
    approval: dict | None
    approval_id: str | None
    ticket_id: str | None


class EventChannelState(TypedDict, total=False):
    """主图单步 SSE 事件队列：节点只写本步事件，服务层沿 updates 流逐节点转发。"""

    events: list[dict]


class DiagState(
    RequestInputState,
    RoutingState,
    RetrievalState,
    ToolEvidenceState,
    AnswerOutputState,
    ApprovalTicketState,
    EventChannelState,
    total=False,
):
    """主图顶层状态（由各职责域组合而成，顶层键名与类型保持向后兼容）。"""


# ---- 显式状态归属声明（程序引导式契约，供代码边界与单测校验） ----

REQUEST_INPUT_FIELDS: frozenset[str] = frozenset(RequestInputState.__annotations__)
RETRIEVAL_INPUT_FIELDS: frozenset[str] = frozenset(RetrievalInputState.__annotations__)
RETRIEVAL_OWNED_FIELDS: frozenset[str] = frozenset(RetrievalState.__annotations__)
RETRIEVAL_OUTPUT_FIELDS: frozenset[str] = RETRIEVAL_OWNED_FIELDS | frozenset({"events"})

RETRIEVAL_SUBGRAPH_NODE_WRITES: dict[str, frozenset[str]] = {
    "retrieve": frozenset({"hits", "events"}),
    "grade": frozenset({"docs_sufficient", "iteration", "range_coverage", "events"}),
    "rewrite": frozenset({"rewritten_query", "events"}),
}

MAIN_GRAPH_NODE_WRITES: dict[str, frozenset[str]] = {
    "route_intent": frozenset(DiagState.__annotations__) - REQUEST_INPUT_FIELDS,
    "pure_range_answer": frozenset({
        "answer",
        "citations",
        "citations_indexed",
        "used_evidence",
        "range_assembly",
        "range_stats",
        "range_coverage",
        "strategy",
        "end_reason",
        "events",
    }),
    "native_tools": frozenset({
        "answer",
        "citations",
        "citations_indexed",
        "hits",
        "docs_sufficient",
        "end_reason",
        "tool_events",
        "ticket_draft",
        "events",
    }),
    "personal_support": frozenset({
        "answer",
        "support_report",
        "hits",
        "end_reason",
        "citations",
        "citations_indexed",
        "events",
    }),
    "call_tools": frozenset({"tool_events", "fault_records", "metrics", "events"}),
    "generate": frozenset({
        "answer",
        "citations",
        "citations_indexed",
        "generation_policy",
        "used_evidence",
        "range_assembly",
        "range_stats",
        "range_coverage",
        "ticket_draft",
        "events",
    }),
    "prepare_approval": frozenset({"approval_id", "ticket_draft"}),
    "approval_gate": frozenset({"approval", "approval_id", "events"}),
    "create_ticket": frozenset({"ticket_id", "answer", "events"}),
}

NODE_STATE_WRITES: dict[str, frozenset[str]] = {
    **MAIN_GRAPH_NODE_WRITES,
    **RETRIEVAL_SUBGRAPH_NODE_WRITES,
}

# 明确主图 DiagState 全部 34 个字段的业务归属（caller / retrieval_subgraph / main_graph）
STATE_FIELD_OWNERS: dict[str, tuple[str, ...]] = {
    **{field: ("caller",) for field in sorted(REQUEST_INPUT_FIELDS)},
    "intent": ("main_graph:route_intent",),
    "strategy": ("main_graph:route_intent", "main_graph:pure_range_answer"),
    "end_reason": (
        "main_graph:route_intent",
        "main_graph:pure_range_answer",
        "main_graph:native_tools",
        "main_graph:personal_support",
    ),
    "rewritten_query": ("retrieval_subgraph:rewrite",),
    "hits": ("retrieval_subgraph:retrieve", "main_graph:native_tools", "main_graph:personal_support"),
    "docs_sufficient": ("retrieval_subgraph:grade", "main_graph:native_tools"),
    "iteration": ("retrieval_subgraph:grade",),
    "range_coverage": ("retrieval_subgraph:grade", "main_graph:pure_range_answer", "main_graph:generate"),
    "tool_events": ("main_graph:call_tools", "main_graph:native_tools"),
    "fault_records": ("main_graph:call_tools",),
    "metrics": ("main_graph:call_tools",),
    "answer": (
        "main_graph:generate",
        "main_graph:pure_range_answer",
        "main_graph:native_tools",
        "main_graph:personal_support",
        "main_graph:create_ticket",
    ),
    "generation_policy": ("main_graph:generate",),
    "citations": (
        "main_graph:generate",
        "main_graph:pure_range_answer",
        "main_graph:native_tools",
        "main_graph:personal_support",
    ),
    "citations_indexed": (
        "main_graph:generate",
        "main_graph:pure_range_answer",
        "main_graph:native_tools",
        "main_graph:personal_support",
    ),
    "used_evidence": ("main_graph:generate", "main_graph:pure_range_answer"),
    "range_assembly": ("main_graph:generate", "main_graph:pure_range_answer"),
    "range_stats": ("main_graph:generate", "main_graph:pure_range_answer"),
    "support_report": ("main_graph:personal_support",),
    "ticket_draft": ("main_graph:generate", "main_graph:native_tools", "main_graph:prepare_approval"),
    "approval": ("main_graph:approval_gate",),
    "approval_id": ("main_graph:prepare_approval", "main_graph:approval_gate"),
    "ticket_id": ("main_graph:create_ticket",),
    "events": ("main_graph:per_node_delta", "retrieval_subgraph:merge_events"),
}


def reset_turn_state() -> dict:
    """同会话新一轮提问时由 route_intent 重置的全部工作流字段（不含 caller 只读输入）。"""
    return {
        # 检索闭环域
        "rewritten_query": "",
        "docs_sufficient": False,
        "iteration": 0,
        "hits": [],
        "range_coverage": None,
        # 工具与证据域
        "tool_events": [],
        "fault_records": [],
        "metrics": {},
        # 回答与输出域
        "answer": "",
        "generation_policy": None,
        "citations": [],
        "citations_indexed": [],
        "used_evidence": None,
        "range_assembly": None,
        "range_stats": None,
        "support_report": None,
        # 审批与工单域
        "ticket_draft": None,
        "approval": None,
        "approval_id": None,
        "ticket_id": None,
        # 路由尾态与单步事件通道
        "events": [],
        "end_reason": "answered",
    }

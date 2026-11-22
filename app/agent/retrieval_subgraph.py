"""规则诊断的检索-评估-改写闭环子图（retrieve -> grade -> rewrite）。

边界与契约：
- 输入状态（RetrievalInputState）：仅读取 question / tenant_id / strategy / rewritten_query / iteration；
  不依赖主图私有字段（session_id / user_id / history / approval / ticket_draft 等）。
- 输出状态（RetrievalOutputState）：仅写入 rewritten_query / hits / docs_sufficient / iteration /
  range_coverage 与 events；绝不越界覆盖主图字段。
- 两种组装方式共用同一套节点逻辑与路由：
  1. build_retrieval_subgraph(nodes)：独立编译的 LangGraph StateGraph(RetrievalSubgraphState)，
     通过 merge_events reducer 按顺序累加闭环内全部事件，可供独立调用或复用验证。
  2. attach_retrieval_subgraph(graph, nodes, exit_node="call_tools")：挂载进主图 DiagState，
     保留逐节点 updates 流式推送契约（node_status + 单步 events，零重复/零丢失）
     及既有 SqliteSaver/PostgresSaver 检查点在任意断点上的 100% 向后兼容。
"""

from langgraph.graph import END, START, StateGraph

from app.agent.state import (
    RETRIEVAL_INPUT_FIELDS,
    RetrievalInputState,
    RetrievalOutputState,
    RetrievalSubgraphState,
)

RETRIEVAL_ENTRY_NODE = "retrieve"
RETRIEVAL_GRADE_NODE = "grade"
RETRIEVAL_REWRITE_NODE = "rewrite"
RETRIEVAL_NODES = (RETRIEVAL_ENTRY_NODE, RETRIEVAL_GRADE_NODE, RETRIEVAL_REWRITE_NODE)
MAX_RETRIEVAL_ROUNDS = 2


def extract_retrieval_input(state: dict) -> RetrievalInputState:
    """按 RetrievalInputState 边界裁剪输入，隔离主图私有字段。"""
    return {key: state[key] for key in RETRIEVAL_INPUT_FIELDS if key in state}


def retrieve_docs(container, tracer, state: RetrievalInputState) -> dict:
    """检索节点：使用原始问题或改写后的 query 检索知识库，仅写 hits 与单步 retrieval 事件。"""
    query = state.get("rewritten_query") or state["question"]
    settings = container.settings
    with (
        container.db_session() as db,
        tracer.span("retrieval", mode=settings.retrieval_mode, tenant_id=state["tenant_id"]) as span,
    ):
        result = container.retriever.retrieve(db, state["tenant_id"], query, mode=settings.retrieval_mode)
        span.metadata["hits"] = len(result.hits)
        span.metadata["reranked"] = result.reranked
        span.metadata["latency_ms"] = round(result.latency_ms, 1)
    hits = [h.to_dict() for h in result.hits]
    event = {
        "type": "retrieval",
        "mode": result.mode,
        "query": query,
        "latency_ms": round(result.latency_ms, 1),
        "reranked": result.reranked,
        "hits": hits[:5],
        "round": state.get("iteration", 0) + 1,
        "strategy": state.get("strategy", "hybrid"),
    }
    if state.get("strategy") == "range_hybrid":
        # 混合诉求组合的策略说明（P1-QUALITY-20261102 必做A）：统计已由程序在
        # 权威文档上完成；检索仅为语义解释补充上下文
        event["why_retrieval"] = (
            "显式行范围的统计已由程序在租户文档权威原文上完成（不消耗检索）；"
            "本查询还包含语义解释诉求，混合检索用于补充相关上下文"
        )
    return {
        "hits": hits,
        "events": [event],
    }


def grade_docs(container, state: RetrievalSubgraphState) -> dict:
    """相关性与目标行覆盖评估节点：仅写 docs_sufficient / iteration / range_coverage 与单步 grade 事件。"""
    sufficient = container.llm.is_sufficient(state["question"], state.get("hits", []))
    iteration = state.get("iteration", 0) + 1
    event = {"type": "grade", "docs_sufficient": sufficient, "iteration": iteration}
    # "相关"与"足以回答"分开（P1-ARAG-20261102）：问题引用显式行范围时，
    # 目标行覆盖必须实际参与充足性决策——有相关日志但缺目标行段不能判充足。
    # 缺失/不完整行的定向补取走租户文档读取（确定性来源，免 embedding/rerank）；
    # 文档亦无此行时无法靠再次检索补齐，带缺口进入生成并由回答如实说明。
    range_coverage = None
    from app.rag.evidence import _resolve_target_lines, parse_line_targets, tenant_docs

    targets = parse_line_targets(state["question"])
    if targets:
        with container.db_session() as db:
            docs = tenant_docs(db, state["tenant_id"])
        found, incomplete = _resolve_target_lines(targets, state.get("hits", []), docs)
        missing = [t for t in targets if t not in found]
        range_coverage = {
            "targets": len(targets),
            "resolved": len(found),
            "missing": missing,
            "incomplete": incomplete,
            "sufficient": not missing and not incomplete,
        }
        if missing or incomplete:
            sufficient = False
            event["range_coverage"] = {**range_coverage, "decision": "insufficient_until_complete"}
        else:
            event["range_coverage"] = range_coverage
    event["docs_sufficient"] = sufficient
    return {
        "docs_sufficient": sufficient,
        "iteration": iteration,
        "range_coverage": range_coverage,
        "events": [event],
    }


def route_after_grade(state: RetrievalSubgraphState) -> str:
    """评估后的闭环分支：最多 1 轮补充检索，范围缺口不做无目的复检。"""
    if state.get("iteration", 0) >= MAX_RETRIEVAL_ROUNDS:
        return "tools"
    cov = state.get("range_coverage")
    if cov and (cov.get("missing") or cov.get("incomplete")):
        # 缺目标行段：定向补取已按文档读取完成且为确定性来源，再次整套检索
        # 不会改变文档内容——带缺口进入生成，由回答如实说明，不做无目的补查
        return "tools"
    if state.get("docs_sufficient"):
        return "tools"
    return "rewrite"


def rewrite_query(container, tracer, state: RetrievalInputState) -> dict:
    """查询改写节点：仅写 rewritten_query 与单步 rewrite 事件。"""
    with tracer.span("rewrite_query"):
        rewritten = container.llm.rewrite_query(state["question"])
    return {
        "rewritten_query": rewritten,
        "events": [{"type": "rewrite", "rewritten": rewritten}],
    }


def _add_retrieval_topology(graph: StateGraph, nodes, *, exit_target) -> str:
    """向目标 StateGraph 注册统一的检索-评估-改写闭环拓扑，返回入口节点名。"""
    graph.add_node(RETRIEVAL_ENTRY_NODE, nodes.retrieve)
    graph.add_node(RETRIEVAL_GRADE_NODE, nodes.grade)
    graph.add_node(RETRIEVAL_REWRITE_NODE, nodes.rewrite)

    graph.add_edge(RETRIEVAL_ENTRY_NODE, RETRIEVAL_GRADE_NODE)
    graph.add_conditional_edges(
        RETRIEVAL_GRADE_NODE,
        nodes.route_after_grade,
        {"rewrite": RETRIEVAL_REWRITE_NODE, "tools": exit_target},
    )
    graph.add_edge(RETRIEVAL_REWRITE_NODE, RETRIEVAL_ENTRY_NODE)
    return RETRIEVAL_ENTRY_NODE


def build_retrieval_subgraph(nodes, *, checkpointer=None):
    """编译独立可运行的检索闭环子图（含显式输入/输出边界与 merge_events reducer）。"""
    subgraph = StateGraph(
        RetrievalSubgraphState,
        input_schema=RetrievalInputState,
        output_schema=RetrievalOutputState,
    )
    entry = _add_retrieval_topology(subgraph, nodes, exit_target=END)
    subgraph.add_edge(START, entry)
    return subgraph.compile(checkpointer=checkpointer)


def attach_retrieval_subgraph(graph: StateGraph, nodes, *, exit_node: str = "call_tools") -> str:
    """将检索闭环子图拓扑显式挂载到主图，保留逐节点流式事件顺序与旧检查点断点兼容。"""
    return _add_retrieval_topology(graph, nodes, exit_target=exit_node)

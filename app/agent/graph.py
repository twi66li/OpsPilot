"""组装诊断工作流；由容器注入持久化检查点，离线测试默认内存。"""
from langgraph.graph import END, START, StateGraph

from app.agent.state import DiagState


def build_graph(nodes, checkpointer=None):
    try:
        from langgraph.checkpoint.memory import MemorySaver
    except ImportError:  # 兼容不同版本的导出名
        from langgraph.checkpoint.memory import InMemorySaver as MemorySaver

    graph = StateGraph(DiagState)
    graph.add_node("route_intent", nodes.route_intent)
    graph.add_node("pure_range_answer", nodes.pure_range_answer)
    graph.add_node("native_tools", nodes.native_tools)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("grade", nodes.grade)
    graph.add_node("rewrite", nodes.rewrite)
    graph.add_node("call_tools", nodes.call_tools)
    graph.add_node("generate", nodes.generate)
    graph.add_node("prepare_approval", nodes.prepare_approval)
    graph.add_node("approval_gate", nodes.approval_gate)
    graph.add_node("create_ticket", nodes.create_ticket)

    graph.add_edge(START, "route_intent")
    graph.add_conditional_edges("route_intent", nodes.select_path,
                                {"pure": "pure_range_answer", "rule": "retrieve", "native": "native_tools"})
    graph.add_edge("pure_range_answer", END)
    graph.add_conditional_edges("native_tools", nodes.route_after_native, {"approval": "prepare_approval", "end": END})
    graph.add_edge("retrieve", "grade")
    graph.add_conditional_edges("grade", nodes.route_after_grade, {"rewrite": "rewrite", "tools": "call_tools"})
    graph.add_edge("rewrite", "retrieve")
    graph.add_edge("call_tools", "generate")
    graph.add_edge("generate", "prepare_approval")
    graph.add_edge("prepare_approval", "approval_gate")
    graph.add_conditional_edges("approval_gate", nodes.route_after_approval, {"ticket": "create_ticket", "end": END})
    graph.add_edge("create_ticket", END)

    return graph.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())

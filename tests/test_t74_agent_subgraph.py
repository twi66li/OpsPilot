"""T74 验收测试：Agent 状态归属、检索闭环子图、事件一致性、旧检查点兼容与审批恢复。

覆盖核心验收项：
1. 状态域拆分、字段归属契约与节点写权限边界；
2. 独立检索闭环子图（retrieve -> grade -> rewrite）的输入/输出过滤与 merge_events reducer；
3. 主图事件序列与原扁平基线行为逐条一致（单轮充足 & 改写补充检索，updates / [updates, custom]）；
4. 模拟旧格式检查点（停在 retrieve/grade/rewrite/approval_gate 断点及同会话新一轮）无缝续跑；
5. 审批 interrupt 中断后（重新实例化图并沿用同一 checkpointer / 重开持久化连接）恢复并完成建单或拒绝。
"""

import sqlite3
from dataclasses import replace
from typing import TypedDict, get_type_hints

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.agent.graph import build_graph, build_retrieval_subgraph
from app.agent.nodes import DiagnosisNodes
from app.agent.retrieval_subgraph import extract_retrieval_input
from app.agent.state import (
    NODE_STATE_WRITES,
    REQUEST_INPUT_FIELDS,
    RETRIEVAL_INPUT_FIELDS,
    RETRIEVAL_OUTPUT_FIELDS,
    RETRIEVAL_OWNED_FIELDS,
    STATE_FIELD_OWNERS,
    DiagState,
    merge_events,
    reset_turn_state,
)
from app.auth.deps import AuthUser
from app.container import get_container
from app.db.base import SessionLocal
from app.db.models import ChatSession, Ticket
from app.services import agent_service
from tests.conftest import new_session

AUTH = AuthUser(user_id="t74-admin", username="admin", tenant_id="default", role="admin")

# 基线 b870635 的顶层 DiagState 全部 34 个键（用于校验 100% 向后兼容）
LEGACY_DIAG_STATE_KEYS = frozenset({
    "question",
    "session_id",
    "tenant_id",
    "user_id",
    "username",
    "history",
    "run_id",
    "evaluation_time",
    "end_reason",
    "support_context",
    "support_report",
    "support_workflow",
    "strategy",
    "intent",
    "rewritten_query",
    "hits",
    "docs_sufficient",
    "iteration",
    "tool_events",
    "fault_records",
    "metrics",
    "answer",
    "generation_policy",
    "citations",
    "citations_indexed",
    "used_evidence",
    "range_assembly",
    "range_coverage",
    "range_stats",
    "ticket_draft",
    "approval",
    "approval_id",
    "ticket_id",
    "events",
})


class LegacyFlatDiagState(TypedDict, total=False):
    """等价于 b870635 拆分前的扁平 DiagState 定义，用于构造旧格式检查点与事件基线。"""

    question: str
    session_id: str
    tenant_id: str
    user_id: str
    username: str
    history: list[dict]
    run_id: str
    evaluation_time: float | None
    end_reason: str
    support_context: dict | None
    support_report: dict | None
    support_workflow: str
    strategy: str
    intent: str
    rewritten_query: str
    hits: list[dict]
    docs_sufficient: bool | None
    iteration: int
    tool_events: list[dict]
    fault_records: list[dict]
    metrics: dict
    answer: str
    generation_policy: str | None
    citations: list[str]
    citations_indexed: list[str]
    used_evidence: list[dict] | None
    range_assembly: dict | None
    range_coverage: dict | None
    range_stats: dict | None
    ticket_draft: dict | None
    approval: dict | None
    approval_id: str | None
    ticket_id: str | None
    events: list[dict]


def _build_legacy_flat_graph(nodes, checkpointer):
    """等价于 b870635 拆分前的主图扁平接线，用于对照事件序列与写入旧检查点。"""
    graph = StateGraph(LegacyFlatDiagState)
    graph.add_node("route_intent", nodes.route_intent)
    graph.add_node("pure_range_answer", nodes.pure_range_answer)
    graph.add_node("native_tools", nodes.native_tools)
    graph.add_node("personal_support", nodes.personal_support)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("grade", nodes.grade)
    graph.add_node("rewrite", nodes.rewrite)
    graph.add_node("call_tools", nodes.call_tools)
    graph.add_node("generate", nodes.generate)
    graph.add_node("prepare_approval", nodes.prepare_approval)
    graph.add_node("approval_gate", nodes.approval_gate)
    graph.add_node("create_ticket", nodes.create_ticket)

    graph.add_edge(START, "route_intent")
    graph.add_conditional_edges(
        "route_intent",
        nodes.select_path,
        {
            "pure": "pure_range_answer",
            "rule": "retrieve",
            "native": "native_tools",
            "personal": "personal_support",
        },
    )
    graph.add_edge("personal_support", END)
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
    return graph.compile(checkpointer=checkpointer)


def _normalize_events(events: list[dict]) -> list[dict]:
    """剔除检索/工具耗时等非确定性字段，用于逐条比对事件序列。"""
    normalized = []
    for event in events:
        item = dict(event)
        if item.get("type") == "retrieval":
            item.pop("latency_ms", None)
        elif item.get("type") == "tool":
            item.pop("duration_ms", None)
        normalized.append(item)
    return normalized


def _make_input_state(session_id: str, question: str, user_id: str = "t74-user") -> dict:
    return {
        "question": question,
        "session_id": session_id,
        "tenant_id": "default",
        "user_id": user_id,
        "username": "admin",
        "history": [],
        "run_id": "t74-run",
        "evaluation_time": None,
        "support_context": None,
        "support_report": None,
        "support_workflow": "auto",
    }


def _sqlite_container(path, *, builder=build_graph):
    conn = sqlite3.connect(str(path), check_same_thread=False)
    container = replace(get_container(), checkpoint_connection=conn)
    nodes = DiagnosisNodes(container)
    container.graph = builder(nodes, SqliteSaver(conn))
    return container, conn


# ---- 1. 状态分层、归属契约与写权限边界 ----


def test_diag_state_schema_ownership_and_turn_reset():
    """DiagState 顶层字段保持 100% 兼容，所有字段均有唯一明确的归属声明。"""
    diag_keys = frozenset(get_type_hints(DiagState))
    assert diag_keys == LEGACY_DIAG_STATE_KEYS
    assert frozenset(STATE_FIELD_OWNERS) == diag_keys
    assert all(owners for owners in STATE_FIELD_OWNERS.values())

    # 检索子图写字段绝不侵入调用方只读输入或审批工单字段
    assert RETRIEVAL_OWNED_FIELDS.isdisjoint(REQUEST_INPUT_FIELDS)
    assert RETRIEVAL_OWNED_FIELDS.isdisjoint({"ticket_draft", "approval", "approval_id", "ticket_id"})
    assert RETRIEVAL_OUTPUT_FIELDS == RETRIEVAL_OWNED_FIELDS | {"events"}

    # reset_turn_state 与 route_intent 同源重置，每次调用返回独立容器
    first_reset = reset_turn_state()
    second_reset = reset_turn_state()
    assert frozenset(first_reset) | {"intent", "strategy"} == diag_keys - REQUEST_INPUT_FIELDS
    first_reset["hits"].append({"chunk_id": "mutated"})
    assert second_reset["hits"] == []


def test_merge_events_reducer_is_pure_and_order_preserving():
    base = [{"type": "retrieval", "round": 1}]
    incoming = [{"type": "grade", "iteration": 1}, None, {}]
    merged = merge_events(base, incoming)
    assert merged == [{"type": "retrieval", "round": 1}, {"type": "grade", "iteration": 1}]
    assert base == [{"type": "retrieval", "round": 1}]
    assert merge_events(None, [{"type": "rewrite"}]) == [{"type": "rewrite"}]
    assert merge_events(base, None) == base


# ---- 2. 独立检索闭环子图与 reducer 合并 ----


@pytest.mark.parametrize(
    "question,expected_rounds,expected_types",
    [
        (
            "ERR_5003 数据库连接池耗尽应该怎么处理？",
            1,
            ["retrieval", "grade"],
        ),
        (
            "请介绍一下上季度全公司的组织架构调整方案",
            2,
            ["retrieval", "grade", "rewrite", "retrieval", "grade"],
        ),
    ],
)
def test_retrieval_subgraph_standalone_boundaries_and_event_merge(
    client, admin_headers, question, expected_rounds, expected_types
):
    """独立编译的检索子图严格隔离输入/输出边界，并通过 merge_events 无损合并多轮事件。"""
    nodes = DiagnosisNodes(get_container())
    subgraph = build_retrieval_subgraph(nodes)

    raw_state = {
        "question": question,
        "tenant_id": "default",
        "strategy": "hybrid",
        "rewritten_query": "",
        "iteration": 0,
        # 主图私有字段：应被 RetrievalInputState / extract_retrieval_input 隔离
        "session_id": "private-session",
        "approval_id": "private-approval",
        "answer": "private-answer",
    }
    cropped = extract_retrieval_input(raw_state)
    assert frozenset(cropped) == RETRIEVAL_INPUT_FIELDS

    output = subgraph.invoke(raw_state)
    assert frozenset(output) <= RETRIEVAL_OUTPUT_FIELDS
    assert "session_id" not in output and "approval_id" not in output and "answer" not in output
    assert output["iteration"] == expected_rounds
    assert [e["type"] for e in output["events"]] == expected_types

    if expected_rounds == 2:
        assert output["rewritten_query"]
        assert [e["round"] for e in output["events"] if e["type"] == "retrieval"] == [1, 2]
        assert [e["iteration"] for e in output["events"] if e["type"] == "grade"] == [1, 2]


# ---- 3. 验收点①：主图事件序列与原行为一致 + 节点写边界实测 ----


@pytest.mark.parametrize(
    "question,stream_mode",
    [
        ("ERR_5003 数据库连接池耗尽应该怎么处理？", "updates"),
        ("ERR_5003 数据库连接池耗尽应该怎么处理？", ["updates", "custom"]),
        ("请介绍一下上季度全公司的组织架构调整方案", "updates"),
        ("请介绍一下上季度全公司的组织架构调整方案", ["updates", "custom"]),
    ],
)
def test_main_graph_event_sequence_matches_legacy_baseline(client, admin_headers, question, stream_mode):
    """新图与 b870635 扁平基线图在单轮充足与改写补充检索路径下产出完全一致的事件序列。"""
    from langgraph.checkpoint.memory import MemorySaver

    session_id = new_session(client, admin_headers, "T74事件对照")
    container = get_container()
    nodes = DiagnosisNodes(container)
    legacy_graph = _build_legacy_flat_graph(nodes, MemorySaver())
    new_graph = build_graph(nodes, MemorySaver())

    input_state = _make_input_state(session_id, question)
    legacy_chunks = list(
        legacy_graph.stream(input_state, {"configurable": {"thread_id": f"legacy-{session_id}"}}, stream_mode=stream_mode)
    )
    new_chunks = list(
        new_graph.stream(input_state, {"configurable": {"thread_id": f"new-{session_id}"}}, stream_mode=stream_mode)
    )

    # 验证新图每个节点的实际写入键均落在 NODE_STATE_WRITES 声明的归属范围内
    for chunk in new_chunks:
        update_map = chunk[1] if isinstance(chunk, tuple) else chunk
        if isinstance(chunk, tuple) and chunk[0] != "updates":
            continue
        for node_name, delta in update_map.items():
            if node_name == "__interrupt__" or delta is None:
                continue
            assert frozenset(delta) <= NODE_STATE_WRITES[node_name], f"节点 {node_name} 越界写入: {set(delta)}"

    legacy_events = _normalize_events(list(agent_service._iter_graph_events(iter(legacy_chunks))))
    new_events = _normalize_events(list(agent_service._iter_graph_events(iter(new_chunks))))
    assert new_events == legacy_events


# ---- 4. 验收点②：模拟旧格式检查点无缝续跑 ----


@pytest.mark.parametrize(
    "interrupt_before,question,expect_rewrite",
    [
        ("grade", "ERR_5003 数据库连接池耗尽应该怎么处理？", False),
        ("rewrite", "请介绍一下上季度全公司的组织架构调整方案", True),
    ],
)
def test_legacy_checkpoint_mid_retrieval_resumes_with_new_graph(
    tmp_path, client, admin_headers, interrupt_before, question, expect_rewrite
):
    """旧版扁平图停在检索闭环中途（grade 或 rewrite 前）的持久检查点，交由新版图无缝续跑。"""
    cp_path = tmp_path / f"legacy_{interrupt_before}.sqlite3"
    session_id = new_session(client, admin_headers, f"T74旧检查点-{interrupt_before}")
    config = {"configurable": {"thread_id": session_id}}

    legacy_container, legacy_conn = _sqlite_container(cp_path, builder=_build_legacy_flat_graph)
    try:
        legacy_container.graph.invoke(
            _make_input_state(session_id, question),
            config,
            interrupt_before=[interrupt_before],
        )
        paused = legacy_container.graph.get_state(config)
        assert paused.next == (interrupt_before,)
    finally:
        legacy_conn.close()

    new_container, new_conn = _sqlite_container(cp_path, builder=build_graph)
    try:
        before_resume = new_container.graph.get_state(config)
        assert before_resume.next == (interrupt_before,)

        resumed_events = list(
            agent_service._iter_graph_events(new_container.graph.stream(None, config, stream_mode="updates"))
        )
        final_state = new_container.graph.get_state(config)
        assert not final_state.next
        assert final_state.values["answer"]

        node_names = [e["node"] for e in resumed_events if e["type"] == "node_status"]
        assert node_names[0] == interrupt_before
        assert "call_tools" in node_names and "generate" in node_names
        assert ("rewrite" in node_names) is expect_rewrite
    finally:
        new_conn.close()


def test_legacy_checkpoint_at_approval_and_next_turn_work_with_new_graph(tmp_path, client, admin_headers):
    """旧图写入的审批中断检查点可由新图恢复建单，且同会话后续新一轮提问无状态污染。"""
    cp_path = tmp_path / "legacy_approval.sqlite3"
    session_id = new_session(client, admin_headers, "T74旧检查点审批")
    with SessionLocal() as db:
        user_id = db.get(ChatSession, session_id).user_id

    # 1) 旧图执行至 approval_gate 触发 interrupt 并落盘
    legacy_container, legacy_conn = _sqlite_container(cp_path, builder=_build_legacy_flat_graph)
    try:
        with SessionLocal() as db:
            started = agent_service.chat_once(
                legacy_container, db, "default", user_id, "admin", session_id, "订单服务又超时了，请帮我创建工单跟进处理。"
            )
        assert started["status"] == "approval_required"
        approval_id = started["approval"]["approval_id"]
    finally:
        legacy_conn.close()

    # 2) 使用新版图重开同一 SQLite 检查点并恢复审批完成建单
    new_container, new_conn = _sqlite_container(cp_path, builder=build_graph)
    try:
        with SessionLocal() as db:
            decided = agent_service.decide_approval(new_container, db, AUTH, approval_id, True, "旧检查点新图放行")
            assert decided["approval_status"] == "approved"
            assert decided["ticket_id"]
            assert db.query(Ticket).filter(Ticket.approval_id == approval_id).count() == 1
        assert not new_container.graph.get_state({"configurable": {"thread_id": session_id}}).next

        # 3) 在同一 thread_id 上由新图继续新一轮普通诊断，验证旧审批/工单状态已干净重置
        with SessionLocal() as db:
            next_turn = agent_service.chat_once(
                new_container, db, "default", user_id, "admin", session_id, "ERR_5003 数据库连接池耗尽应该怎么处理？"
            )
        assert next_turn["status"] == "answered"
        assert next_turn["intent"] == "diagnose"
        assert next_turn["ticket_id"] is None
        assert "连接池" in next_turn["answer"]
    finally:
        new_conn.close()


# ---- 5. 验收点③：审批 interrupt 中断后重新实例化图并沿用同一 checkpointer 恢复 ----


@pytest.mark.parametrize("approved", [True, False])
def test_approval_interrupt_resumes_after_reinstantiating_graph_same_checkpointer(
    tmp_path, client, admin_headers, approved
):
    """审批 interrupt 暂停后，重新实例化图并沿用同一 checkpointer，正常恢复并完成后续节点。"""
    cp_path = tmp_path / f"approval_reinst_{approved}.sqlite3"
    session_id = new_session(client, admin_headers, f"T74审批恢复-{approved}")
    with SessionLocal() as db:
        user_id = db.get(ChatSession, session_id).user_id

    conn = sqlite3.connect(str(cp_path), check_same_thread=False)
    shared_checkpointer = SqliteSaver(conn)
    try:
        # 第一次实例化图并触发 approval_gate interrupt
        first_container = replace(get_container(), checkpoint_connection=conn)
        first_container.graph = build_graph(DiagnosisNodes(first_container), shared_checkpointer)

        with SessionLocal() as db:
            started = agent_service.chat_once(
                first_container,
                db,
                "default",
                user_id,
                "admin",
                session_id,
                "订单服务又超时了，请帮我创建工单跟进处理。",
            )
        assert started["status"] == "approval_required"
        approval_id = started["approval"]["approval_id"]
        assert any(e["type"] == "approval_required" for e in started["events"])

        paused = first_container.graph.get_state({"configurable": {"thread_id": session_id}})
        assert paused.next == ("approval_gate",)

        # 重新实例化 DiagnosisNodes 与图，沿用同一 checkpointer 实例
        rebuilt_container = replace(get_container(), checkpoint_connection=conn)
        rebuilt_container.graph = build_graph(DiagnosisNodes(rebuilt_container), shared_checkpointer)
        assert rebuilt_container.graph is not first_container.graph

        with SessionLocal() as db:
            decided = agent_service.decide_approval(
                rebuilt_container, db, AUTH, approval_id, approved, "T74重实例化恢复验证"
            )
            assert decided["approval_status"] == ("approved" if approved else "rejected")
            assert bool(decided["ticket_id"]) is approved
            assert db.query(Ticket).filter(Ticket.approval_id == approval_id).count() == int(approved)

        # 验证恢复阶段的节点与事件序列完整且图运行彻底结束
        resumed_nodes = [e["node"] for e in decided["events"] if e["type"] == "node_status"]
        resumed_types = [e["type"] for e in decided["events"]]
        assert resumed_nodes == ["approval_gate", "create_ticket"]
        assert "approval_decision" in resumed_types
        expected_outcome = "ticket_created" if approved else "ticket_rejected"
        assert expected_outcome in resumed_types
        assert not rebuilt_container.graph.get_state({"configurable": {"thread_id": session_id}}).next
    finally:
        conn.close()

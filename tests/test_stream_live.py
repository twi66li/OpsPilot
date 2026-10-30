"""回归：SSE 过程事件必须边到边推送——首个 node_status 到达时图尚未被整图耗尽。"""
import json
from contextlib import contextmanager

from app.services import agent_service


class FakeDB:
    """最小 DB 桩：查询链返回空，写入操作 no-op。"""

    def query(self, *args, **kwargs):
        return _FakeQuery()

    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return None

        return _noop


class _FakeQuery:
    def filter(self, *args, **kwargs):
        return self

    def filter_by(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def first(self):
        return None

    def all(self):
        return []


class FakeGraph:
    """两个节点的可控图：stream() 全部耗尽时打标记，供断言事件与耗尽的先后。"""

    def __init__(self):
        self.exhausted = False

    def stream(self, input_state, config, stream_mode=None):
        def _chunks():
            yield {"retrieve": {"events": [{"type": "retrieval", "docs": []}]}}
            yield {"generate": {"events": [{"type": "answer", "answer": "处理完成"}]}}

        for chunk in _chunks():
            yield chunk
        self.exhausted = True

    def get_state(self, config):
        class _Snapshot:
            next = ()
            values = {"answer": "处理完成", "citations": [], "intent": "diagnose", "ticket_id": None}

        return _Snapshot()


class FakeContainer:
    def __init__(self, graph):
        from types import SimpleNamespace

        self.graph = graph
        self.settings = SimpleNamespace(agent_mode="rule")

    @contextmanager
    def db_session(self):
        yield FakeDB()


def test_process_events_sent_before_graph_exhausted():
    graph = FakeGraph()
    container = FakeContainer(graph)
    gen = agent_service.chat_stream(container, "tenant-a", "u1", "ops", "sess-1", "网关 502 如何排查")

    saw_node_status = False
    for raw in gen:
        event = json.loads(raw[len("data: ") :])
        if event.get("type") == "node_status":
            assert not graph.exhausted, "首个 node_status 到达时图已被整图耗尽（仍是缓存补发行为）"
            saw_node_status = True
            break

    assert saw_node_status, "流中未产出任何 node_status 事件"
def test_native_interrupt_tuple_emits_approval_event():
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    from app.services.agent_service import _iter_graph_events

    graph = StateGraph(dict)
    graph.add_node("approval", lambda state: interrupt({"approval_id": "native-approval", "ticket_draft": {}}))
    graph.add_edge(START, "approval")
    graph.add_edge("approval", END)
    compiled = graph.compile(checkpointer=MemorySaver())
    stream = compiled.stream({}, {"configurable": {"thread_id": "native-interrupt"}}, stream_mode="updates")
    assert any(event["type"] == "approval_required" for event in _iter_graph_events(stream))

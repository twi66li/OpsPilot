import json
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.agent.state import DiagState
from app.container import get_container
from app.services import agent_service, background_runs
from app.services.run_service import RunRecorder
from tests.conftest import new_session


def read_frames(response):
    assert response.status_code == 200, response.text
    return [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")]


def test_background_run_replays_events_with_cursor_and_reuses_request(client, admin_headers):
    session_id = new_session(client, admin_headers)
    body = {"session_id": session_id, "message": "排查 ERR_5003", "request_id": "background-request-01", "background": True}
    response = client.post("/api/agent/chat/stream", json=body, headers=admin_headers)
    original = read_frames(response)
    run_id = original[0]["run_id"]
    assert original[0]["type"] == "connected"
    answer = next(event for event in original if event["type"] == "final")
    assert answer["status"] == "answered"
    last_cursor = [line.split()[1] for line in response.text.splitlines() if line.startswith("id:")][-1]
    replay = read_frames(client.get(f"/api/agent/runs/{run_id}/events?after={last_cursor}", headers=admin_headers))
    assert [event["type"] for event in replay] == ["connected", "final", "done"]
    assert replay[1] == answer
    duplicate = read_frames(client.post("/api/agent/chat/stream", json=body, headers=admin_headers))
    assert duplicate[0]["run_id"] == run_id
    history = client.get(f"/api/sessions/{session_id}", headers=admin_headers).json()
    assert history["active_run_id"] is None
    assert [message["role"] for message in history["messages"]] == ["user", "assistant"]


def test_background_run_resumes_persisted_checkpoint(client, admin_headers, monkeypatch, tmp_path):
    container = get_container()
    session_id = new_session(client, admin_headers)
    question = "请从已保存的证据继续"
    recorder = RunRecorder.start(container, "default", session_id, question, "background-restart-01", background=True)
    with container.db_session() as db:
        from app.db.models import AgentRun, ChatSession

        user_id = db.get(ChatSession, session_id).user_id
        input_state = agent_service._background_input(
            db, db.get(AgentRun, recorder.id), "default", session_id,
            {"question": question, "run_id": recorder.id, "session_id": session_id, "tenant_id": "default", "user_id": user_id},
        )

    completed = []

    def collect(state):
        completed.append("collect")
        return {"rewritten_query": state["question"]}

    def conclude(state):
        completed.append("conclude")
        return {"answer": state["rewritten_query"], "end_reason": "answered"}

    graph = StateGraph(DiagState)
    graph.add_node("collect", collect)
    graph.add_node("conclude", conclude)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "conclude")
    graph.add_edge("conclude", END)
    config = {"configurable": {"thread_id": session_id}}
    path = tmp_path / "checkpoints.sqlite"
    connection = sqlite3.connect(path, check_same_thread=False)
    try:
        graph.compile(checkpointer=SqliteSaver(connection)).invoke(input_state, config, interrupt_before=["conclude"], durability="sync")
    finally:
        connection.close()
    assert completed == ["collect"]
    active = client.get(f"/api/sessions/{session_id}", headers=admin_headers).json()
    assert active["active_run_id"] == recorder.id

    restarted = sqlite3.connect(path, check_same_thread=False)
    try:
        monkeypatch.setattr(container, "graph", graph.compile(checkpointer=SqliteSaver(restarted)))
        monkeypatch.setattr(background_runs, "_worker", None)
        background_runs.start_worker(container)
        background_runs.stop_worker()
    finally:
        restarted.close()
    assert completed == ["collect", "conclude"]
    history = client.get(f"/api/sessions/{session_id}", headers=admin_headers).json()
    assert history["active_run_id"] is None
    assert [message["role"] for message in history["messages"]] == ["user", "assistant"]
    assert history["messages"][1]["content"] == question
    assert history["messages"][1]["meta"]["run_id"] == recorder.id

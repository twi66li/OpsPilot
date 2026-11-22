import select
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.agent import tool_loop
from app.container import get_container
from app.db.base import SessionLocal
from app.db.models import AgentRun
from app.services.budget import BudgetLedger
from app.services.model_api import ChatAPI
from app.services.run_service import RunCancelled, RunRecorder, capture_run
from tests.agent.test_tool_loop import ScriptedAPI, tool
from tests.conftest import new_session


def test_cancel_slow_model_closes_response_and_marks_unknown(client, admin_headers, tmp_path, monkeypatch):
    started = threading.Event()
    disconnected = threading.Event()

    class SlowModel(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.flush()
            started.set()
            if select.select([self.connection], [], [], 30)[0] and self.connection.recv(1) == b"":
                disconnected.set()

    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ledger = BudgetLedger(str(tmp_path / "cancel.jsonl"), 10000, {"offline-test": 10000})
    try:
        api = ChatAPI("offline", f"http://127.0.0.1:{server.server_port}/v1", "offline-test", budget_guard=ledger)
        session_id = new_session(client, admin_headers)
        recorder = RunRecorder.start(get_container(), "default", session_id, "检查连接池")

        def work():
            with capture_run(recorder):
                try:
                    api.chat("系统", "测试取消")
                except RunCancelled:
                    # 模拟取消和迟到输出竞争写入终态。
                    recorder.finish({"status": "answered", "answer": "迟到回答"})
                    return
                raise AssertionError("模型流没有取消")

        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(work)
            assert started.wait(8)
            requested_at = time.monotonic()
            response = client.post(f"/api/agent/runs/{recorder.id}/cancel", headers=admin_headers)
            assert response.status_code == 200, response.text
            assert response.json()["status"] in ("cancel_requested", "cancelled")
            assert time.monotonic() - requested_at < 2
            assert disconnected.wait(2)
            running.result(timeout=2)
        with SessionLocal() as db:
            row = db.get(AgentRun, recorder.id)
            assert row.status == "cancelled"
            assert row.data["result"] == {"status": "cancelled", "end_reason": "user_cancelled"}
        assert ledger.unknown > 0 and ledger.actual == ledger.in_flight == 0
    finally:
        ledger.close()
        server.shutdown()
        server.server_close()


def test_timeout_cancels_queued_tool_and_reuses_worker(monkeypatch):
    slow_started, release, finished = threading.Event(), threading.Event(), threading.Event()
    ran = []

    def execute(name, args):
        ran.append((args["query"], threading.get_ident()))
        if args["query"] == "先运行":
            slow_started.set()
            release.wait(3)
            finished.set()
        return {"status": "ok", "hits": []}

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(tool_loop, "_READ_POOL", pool)
        first = ScriptedAPI({"tool_calls": [
            tool("search_runbook", {"query": "先运行"}, "slow"),
            tool("search_runbook", {"query": "队列任务"}, "queued"),
        ]})
        try:
            result = tool_loop.run_tool_loop("测试", first, execute, timeout=0.1)
            assert slow_started.is_set() and result["end_reason"] == "tool_timeout"
        finally:
            release.set()
        assert finished.wait(2)
        next_call = ScriptedAPI(
            {"tool_calls": [tool("search_runbook", {"query": "成功复用"}, "second")]},
            {"content": "完成"},
        )
        assert tool_loop.run_tool_loop("测试", next_call, execute)["end_reason"] == "answered"
        assert [query for query, _ in ran] == ["先运行", "成功复用"]
        assert ran[0][1] == ran[1][1]

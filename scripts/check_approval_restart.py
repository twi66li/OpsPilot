"""验收已启动的本地 Compose：真实审批跨 API 重启恢复、并发幂等和权限隔离。

运行：python scripts/check_approval_restart.py [eval/reports/输出目录]
使用 seed 演示账号；保留新增验收会话/工单，不删除数据，不重启电脑。
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

BASE = "http://127.0.0.1:8000"
DOCKER = shutil.which("docker") or r"D:\Docker\resources\bin\docker.exe"
PROBE = """
import hashlib,json
from pathlib import Path
from sqlalchemy import inspect,text
from app.config import get_settings
from app.container import get_container
from app.db.base import engine
s=get_settings(); c=get_container()
result={'checkpointer':type(c.graph.checkpointer).__name__,'checkpoint_path':s.checkpoint_path,'strict':s.model_strict,'llm':type(c.llm).__name__,'mysql':engine.dialect.name}
with engine.connect() as db:
    if payload.get('before'):
        before=payload['before']
        ids=set(before['ticket_ids'])
        columns=before['ticket_columns']
        rows=[{k:row[k] for k in columns} for row in db.execute(text('SELECT * FROM tickets ORDER BY id')).mappings() if row['id'] in ids]
        result['legacy_rows_unchanged']=hashlib.sha256(json.dumps(rows,sort_keys=True,default=str,ensure_ascii=False).encode()).hexdigest()==before['ticket_rows_sha256']
        result['approval_column_exists']='approval_id' in {c['name'] for c in inspect(engine).get_columns('tickets')}
        result['unique_index']=[i for i in inspect(engine).get_indexes('tickets') if i['name']=='uq_tickets_approval_id']
    if payload.get('approval_ids'):
        result['ticket_counts']={aid:db.execute(text('SELECT COUNT(*) FROM tickets WHERE approval_id=:aid'),{'aid':aid}).scalar_one() for aid in payload['approval_ids']}
    result['total_tickets']=db.execute(text('SELECT COUNT(*) FROM tickets')).scalar_one()
if payload.get('session_id'):
    state=c.graph.get_state({'configurable':{'thread_id':payload['session_id']}})
    result['graph_next']=state.next
    result['graph_approval_id']=state.values.get('approval_id')
    result['checkpoint_file_bytes']=Path(s.checkpoint_path).stat().st_size
print('T9_PROBE '+json.dumps(result,ensure_ascii=False))
"""


def main():
    started = datetime.now(timezone.utc).isoformat()
    report = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("eval/reports") / ("t9_approval_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"))
    report.mkdir(parents=True, exist_ok=True)
    secret = dotenv_values(".env").get("OPENAI_API_KEY", "")
    events = []
    summary = {"status": "running", "started_at": started}

    def save(name, value):
        content = json.dumps(value, ensure_ascii=False, indent=2)
        if secret:
            content = content.replace(secret, "[REDACTED]")
        (report / name).write_text(re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", content), encoding="utf-8")

    def docker(*args, input=None):
        result = subprocess.run([DOCKER, "compose", *args], input=input, capture_output=True, text=True, encoding="utf-8", timeout=60)
        if result.returncode:
            raise RuntimeError(f"Docker command failed: {args[0]}: {result.stderr}")
        return result.stdout + result.stderr if args[0] == "logs" else result.stdout

    def probe(name, payload):
        output = docker("exec", "-T", "api", "python", "-", input=f"payload={payload!r}\n" + PROBE)
        line = next(line for line in output.splitlines() if line.startswith("T9_PROBE "))
        result = json.loads(line[len("T9_PROBE "):])
        save(name, result)
        assert result["checkpointer"] == "SqliteSaver" and result["strict"]
        assert result["mysql"] == "mysql" and result["llm"] == "OpenAIChatLLM"
        return result

    with httpx.Client(timeout=90) as client:
        def ready():
            for _ in range(45):
                try:
                    if client.get(BASE + "/health", timeout=2).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            raise RuntimeError("API health not ready")

        def login(username, password):
            response = client.post(BASE + "/auth/login", json={"username": username, "password": password})
            response.raise_for_status()
            return {"Authorization": "Bearer " + response.json()["access_token"]}

        def call(method, path, headers, body=None, expected=200):
            response = client.request(method, BASE + path, headers=headers, json=body)
            data = response.json()
            events.append({"method": method, "path": path, "request": body, "status": response.status_code, "response": data})
            save("http.json", events)
            assert response.status_code == expected, (path, response.status_code, data)
            return data

        def restart(name):
            before = docker("ps", "-q", "api").strip()
            started_before = subprocess.check_output([DOCKER, "inspect", "--format", "{{.State.StartedAt}}", before], text=True).strip()
            output = docker("restart", "api")
            ready()
            after = docker("ps", "-q", "api").strip()
            assert before == after  # 同一容器的真实进程重启，命名卷继续保留。
            started_after = subprocess.check_output([DOCKER, "inspect", "--format", "{{.State.StartedAt}}", after], text=True).strip()
            assert started_before != started_after
            save(name, {"container_id": after, "command": "docker compose restart api", "exit_code": 0, "output": output, "started_before": started_before, "started_after": started_after, "ready_at": datetime.now(timezone.utc).isoformat()})

        def start_flow(title, message, headers):
            sid = call("POST", "/api/sessions", headers, {"title": title})["id"]
            result = call("POST", "/api/agent/chat", headers, {"session_id": sid, "message": message})
            assert result["status"] == "approval_required"
            return sid, result["approval"]["approval_id"]

        try:
            ready()
            before_file = report / "before_migration.json"
            migration = probe("after_migration.json", {"before": json.loads(before_file.read_text(encoding="utf-8"))} if before_file.exists() else {})
            if before_file.exists():
                assert migration["legacy_rows_unchanged"] and migration["approval_column_exists"]
                assert migration["unique_index"][0]["unique"]
            admin = login("admin", "admin123")
            viewer = login("viewer01", "view123456")
            outsider = login("acme_admin", "acme123456")
            sid, aid = start_flow("T9跨重启批准", "order-service 超时，请创建工单跟进处理。", admin)
            paused = probe("paused_checkpoint.json", {"session_id": sid})
            assert paused["graph_next"] == ["approval_gate"] and paused["graph_approval_id"] == aid
            assert paused["checkpoint_file_bytes"] > 0
            call("POST", f"/api/agent/approvals/{aid}/decide", viewer, {"approved": True}, expected=403)
            call("POST", f"/api/agent/approvals/{aid}/decide", outsider, {"approved": True}, expected=404)
            call("GET", f"/api/sessions/{sid}", outsider, expected=404)
            assert call("GET", f"/api/agent/sessions/{sid}/pending-approval", outsider)["pending"] is False
            restart("approval_restart.json")
            assert call("GET", f"/api/agent/sessions/{sid}/pending-approval", admin)["approval_id"] == aid

            def approve(_):
                response = httpx.post(BASE + f"/api/agent/approvals/{aid}/decide", headers=admin, json={"approved": True, "comment": "T9并发审批"}, timeout=60)
                return {"status": response.status_code, "response": response.json()}

            with ThreadPoolExecutor(max_workers=4) as pool:
                decisions = list(pool.map(approve, range(4)))
            save("concurrent_decisions.json", decisions)
            assert all(item["status"] == 200 for item in decisions)
            ticket_ids = {item["response"]["ticket_id"] for item in decisions}
            assert len(ticket_ids) == 1 and None not in ticket_ids
            ticket_id = next(iter(ticket_ids))
            repeated = call("POST", f"/api/agent/approvals/{aid}/decide", admin, {"approved": True})
            assert repeated["ticket_id"] == ticket_id
            call("POST", f"/api/agent/approvals/{aid}/decide", admin, {"approved": False}, expected=409)
            sid_reject, aid_reject = start_flow("T9跨重启拒绝", "payment-service 对账异常，请创建工单跟进。", admin)
            restart("rejection_restart.json")
            rejected = call("POST", f"/api/agent/approvals/{aid_reject}/decide", admin, {"approved": False, "comment": "验收拒绝分支"})
            assert rejected["approval_status"] == "rejected" and rejected["ticket_id"] is None
            retry_reject = call("POST", f"/api/agent/approvals/{aid_reject}/decide", admin, {"approved": False})
            assert retry_reject["ticket_id"] is None
            call("POST", f"/api/agent/approvals/{aid_reject}/decide", admin, {"approved": True}, expected=409)
            final = probe("final_backend.json", {"approval_ids": [aid, aid_reject], "session_id": sid_reject})
            assert final["ticket_counts"] == {aid: 1, aid_reject: 0} and final["graph_next"] == []
            summary.update(status="passed_pending_zcode_review", approved_approval=aid, rejected_approval=aid_reject, ticket_id=ticket_id, concurrent_requests=4, unique_ticket_count=1, rejected_ticket_count=0, real_api_restarts=2, cross_tenant_blocked=True, viewer_blocked=True, legacy_migration_preserved=migration.get("legacy_rows_unchanged"), scope="单API进程 + 文件型SqliteSaver + 实际MySQL/Redis/Qdrant及真实模型；非多实例部署或分布式原子事务")
        except Exception as exc:
            summary.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        finally:
            logs = docker("logs", "--no-color", "--since", started, "api")
            usage = []
            for line in logs.splitlines():
                match = re.search(r"model_api_usage kind=(\w+) record=(\{.*\})", line)
                if match:
                    usage.append({"kind": match.group(1), **json.loads(match.group(2))})
            save("usage.json", {"calls": usage, "total_tokens": sum(row["usage"].get("total_tokens", 0) for row in usage), "source": "实际API容器自本脚本启动以来的实报usage日志；同一容器两次restart保留日志，不重复计数"})
            summary["completed_at"] = datetime.now(timezone.utc).isoformat()
            save("summary.json", summary)
            save("run_manifest.json", {"baseline": "5e74ea3", "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "artifacts": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in report.iterdir() if f.is_file() and f.name != "run_manifest.json"}})
    print(json.dumps({"report": str(report), **summary}, ensure_ascii=False))
    return 0 if summary["status"].startswith("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

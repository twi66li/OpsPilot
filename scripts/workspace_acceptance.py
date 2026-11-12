"""单次真实模型验收：网站中心→知识咨询→问题记录→检查→程序核对→确认解决与归档。"""
import json
import time
from pathlib import Path

import httpx

from app.config import get_settings


def main():
    settings = get_settings()
    started = time.monotonic()
    stages = []
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=180) as client:
        login = client.post("/auth/login", json={"username": settings.admin_username, "password": settings.admin_password})
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer " + login.json()["access_token"]

        def call(method, path, body=None):
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        site = call("POST", "/api/support/sites", {"name": "维护工作台验收网站（非生产）", "framework": "FastAPI",
                     "deployment": "Docker Compose + Nginx", "database": "PostgreSQL 16", "versions": "Python 3.12"})
        consultation = call("POST", "/api/support/cases", {"title": "咨询如何收集日志", "kind": "consult", "site_id": site["id"]})
        answer = call("POST", "/api/agent/chat", {"session_id": consultation["id"], "workflow": "consult",
                       "message": "用一两句话解释Docker容器中如何只读提取最近10分钟日志，并提醒提交前脱敏。"})
        assert answer.get("support_report", {}).get("workflow") == "consult", answer.get("end_reason")
        assert not answer["support_report"]["steps"] and "结果核对" not in answer["answer"]
        assert consultation["id"] not in {c["id"] for c in call("GET", f"/api/support/cases?kind=problem&site_id={site['id']}")}
        stages.append({"kind": "consult", "run_id": answer["run_id"], "report": answer["support_report"]})
        print("知识咨询通过：不创建故障问题、不显示恢复步骤", flush=True)

        issue = call("POST", "/api/support/cases", {"title": "验收样例：结算页返回502", "site_id": site["id"],
                     "symptom": "发布后GET /checkout返回502；仅验收样例", "endpoint": "/checkout",
                     "impact": "样例结算请求", "recent_changes": "教学样例中调整过网关上游配置"})
        root = "/api/support/cases/" + issue["id"]
        call("POST", root + "/evidence", {"label": "教学样例，非网站实采", "text":
            '127.0.0.1 - - [12/Nov/2026:10:00:00 +0800] "GET /checkout HTTP/1.1" 502 100\n'
            '[error] connect() failed (111: Connection refused) while connecting to upstream'})
        first = call("POST", "/api/agent/chat", {"session_id": issue["id"], "workflow": "diagnose",
                    "message": "请分析提交的502样例，区分已知事实与待验证原因。只给最必要的一步只读检查，不修改配置，不查询演练数据。"})
        assert first.get("support_report", {}).get("steps"), first.get("end_reason")
        assert first["support_report"]["verification"]["verdict"] == "inconclusive"
        stages.append({"kind": "diagnose", "run_id": first["run_id"], "report": first["support_report"]})
        call("POST", root + "/confirm-plan")
        planned = call("GET", root)
        step = call("POST", root + "/plan/steps/0", {"revision": planned["plan"]["revision"], "status": "done",
                    "note": "验收样例：已按建议核对上游配置；此记录本身不能证明恢复"})
        assert step["sample_checks"]["verification"]["verdict"] == "inconclusive"
        call("POST", root + "/evidence", {"kind": "check_result", "label": "处理后验收样例，非生产恢复", "plan_revision": planned["plan"]["revision"], "step_index": 0,
              "text": '\n'.join(f'127.0.0.1 - - [12/Nov/2026:10:10:0{i} +0800] "GET /checkout HTTP/1.1" 200 100' for i in range(3))})
        measured = call("GET", root)["sample_checks"]
        assert measured["verification"]["verdict"] == "sample_recovered"
        assert measured["comparison"]["after_requests"] == 3 and measured["comparison"]["after_failures"] == 0
        print("程序核对通过：同一接口、较晚时间、3次请求、0个HTTP错误", flush=True)
        second = call("POST", "/api/agent/chat", {"session_id": issue["id"], "workflow": "review",
                     "message": "请简短解释程序对照的接口、时间和请求数，说明结果边界即可，不生成新方案；不要声称已经修改真实网站。"})
        assert second.get("support_report", {}).get("verification", {}).get("method") == "program", second.get("end_reason")
        assert second["support_report"]["verification"]["verdict"] == measured["verification"]["verdict"]
        stages.append({"kind": "review", "run_id": second["run_id"], "report": second["support_report"]})

        resolved = call("POST", root + "/status", {"status": "user_resolved", "user_confirmed": True,
                        "note": "仅验收样例：用户确认原接口样本通过，不代表真实线上恢复"})
        assert resolved["status"] == "user_resolved" and not resolved["archived"]
        archived = call("POST", root + "/save", {"solution": resolved["solution"], "user_confirmed": True})
        assert archived["status"] == "user_resolved" and archived["archived"]
        detail = call("GET", "/api/support/sites/" + site["id"])
        assert any(c["id"] == issue["id"] and c["archived"] for c in detail["cases"])
        history = call("POST", "/api/support/cases", {"title": "查询网站的历史处理", "kind": "history", "site_id": site["id"]})
        recalled = call("POST", "/api/agent/chat", {"session_id": history["id"], "workflow": "history",
                         "message": "查询这个网站刚才结算页502的处理记录，简短说明样本核对与用户确认的区别。"})
        assert recalled.get("support_report", {}).get("workflow") == "history", recalled.get("end_reason")
        related = call("GET", "/api/support/cases/" + history["id"])["related_cases"]
        assert any(c["session_id"] == issue["id"] for c in related)
        stages.append({"kind": "history", "run_id": recalled["run_id"], "report": recalled["support_report"]})
        record = {"date": "2026-11-12", "source": "明确标注的验收输入样例；非生产实采", "elapsed_s": round(time.monotonic()-started,2),
                  "site_id": site["id"], "issue_id": issue["id"], "checks": {"consultation_separate":True, "site_issue_linked":True,
                  "step_completion_not_recovery":True, "sample_comparison_by_program":True, "resolution_separate_from_archive":True,
                  "saved_case_recalled_for_same_site":True},
                  "stages": stages, "sample_checks": measured}
        folder = Path("eval/night20261112/maintenance_workspace")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "results.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"record": str(folder / "results.json"), "checks": record["checks"], "elapsed_s": record["elapsed_s"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

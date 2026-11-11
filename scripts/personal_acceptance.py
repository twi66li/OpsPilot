"""单次本机真实模型验收：模拟用户提交的日志→确认方案→新结果→保存案例。"""
import argparse
import json
import time
from pathlib import Path

import httpx

from app.config import get_settings


def main(session_id=None):
    settings = get_settings()
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=180) as client:
        response = client.post("/auth/login", json={"username": settings.admin_username, "password": settings.admin_password})
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]

        def call(method, path, body=None):
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        if session_id:
            case = call("GET", f"/api/support/cases/{session_id}")
        else:
            site = call("POST", "/api/support/sites", {"name": "验收样例网站（非生产）", "framework": "FastAPI / SQLAlchemy",
                                                        "deployment": "Docker Compose", "database": "PostgreSQL 16",
                                                        "versions": "Python 3.12，SQLAlchemy 2.x"})
            case = call("POST", "/api/support/cases", {"title": "个人排障闭环验收：连接池等待超时", "site_id": site["id"]})
        root = f"/api/support/cases/{case['id']}"
        evidence = case["evidence"][0] if case.get("evidence") else call("POST", root + "/evidence", {"label": "验收输入样例，非网站实采", "text":
            "2026-11-12T00:10:01+08:00 POST /orders HTTP 500\n"
            "sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00\n"
            "database_url=postgresql://app:test-password@db/site"})
        print("已准备无私人手册的个人排障会话", case["id"], flush=True)
        started = time.monotonic()
        first = call("POST", "/api/agent/chat", {"session_id": case["id"], "message":
            "请分析提交的验收样例日志，区分日志事实和可能原因，只给最必要的1到2步只读检查，不要查询任何演练日志或指标。"})
        if not first.get("support_report"):
            raise RuntimeError(f"首次报告失败，run_id={first.get('run_id')} end_reason={first.get('end_reason')}")
        assert first["support_report"]["verification"]["verdict"] == "inconclusive"
        assert first["hits"] and all(h.get("source_kind") == "official" for h in first["hits"])
        assert all(e["name"] in ("search_runbook", "search_saved_cases") for e in first["events"] if e.get("type") == "tool")
        print("首次真实模型报告通过，确认方案后提交新的样例结果", flush=True)
        call("POST", root + "/confirm-plan")
        after = call("POST", root + "/evidence", {"kind": "check_result", "label": "验收用反馈样例，非真实线上恢复",
            "collected_at": "2026-11-12 00:20 北京时间；仅三次样例请求",
            "text": "已在样例中修正异常路径未关闭Session的问题。\n"
                    "2026-11-12T00:20:01+08:00 POST /orders HTTP 200 elapsed=120ms\n"
                    "2026-11-12T00:20:02+08:00 POST /orders HTTP 200 elapsed=125ms\n"
                    "2026-11-12T00:20:03+08:00 POST /orders HTTP 200 elapsed=118ms\n"
                    "本次采集窗口00:20:00至00:21:00的应用日志未再出现QueuePool timeout；仅能说明这份提交的样本。"})
        second = call("POST", "/api/agent/chat", {"session_id": case["id"], "message":
            "我提交了新的样例反馈，请核对原症状在提交的样本中是否改善，并说明仍不能确认的部分。不要声称已连接或修复真实网站。"})
        assert second.get("support_report"), second.get("end_reason")
        call("POST", root + "/save", {"solution": "验收样例：排查Session生命周期并提交三次成功请求；只代表输入样本，非线上恢复证明。", "user_confirmed": True})
        record = {"session_id": case["id"], "input_source": "明确标记的用户输入样例；不是生产或故障演练服务日志",
                  "elapsed_s": round(time.monotonic() - started, 2), "initial_evidence_id": evidence["id"],
                  "new_evidence_id": after["id"], "first": first, "followup": second,
                  "saved": call("GET", root)}
        path = Path("data/personal-acceptance-20261112.json")
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"record": str(path), "session_id": case["id"], "elapsed_s": record["elapsed_s"],
                          "first_run": first["run_id"], "followup_run": second["run_id"],
                          "verification": second["support_report"]["verification"], "saved": record["saved"]["status"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", help="从尚未保存的验收会话继续")
    main(parser.parse_args().session_id)

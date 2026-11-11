"""一次连贯演示：真实PDF→知识引用→调查流→审批演练修复，不运行批量评测。"""
import argparse
import hashlib
import io
import json
import time
from pathlib import Path

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.config import get_settings


def demo_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
        NameObject("/F1"): DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                           NameObject("/Subtype"): NameObject("/Type1"),
                                           NameObject("/BaseFont"): NameObject("/Helvetica")})})})
    lines = ["OpsPilot demo remediation guide", "ERR_DB_POOL_TIMEOUT on order-service can be TEST INJECTION.",
             "Compare actual request logs and service metrics in the SAME recent window.",
             "Do not claim a real database pool is exhausted from the injected error code.",
             "After HUMAN APPROVAL, reset_demo_fault clears only the demo injection.",
             "Verify actual requests before and after; this does not prove long-term SLO recovery."]
    stream = DecodedStreamObject()
    stream.set_data(("BT /F1 11 Tf 30 780 Td " + " ".join(f"({line}) Tj 0 -20 Td" for line in lines) + " ET").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--report", default="eval/night20261111/demo_acceptance/results.json")
    args = parser.parse_args()
    settings, report = get_settings(), {}
    with httpx.Client(base_url=args.base_url, timeout=180, trust_env=False) as client:
        response = client.post("/auth/login", json={"username": settings.admin_username, "password": settings.admin_password})
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        pdf = demo_pdf()
        response = client.post("/api/kb/documents/upload", data={"title": "OpsPilot演练修复手册"},
                               files={"file": ("opspilot-demo-guide.pdf", pdf, "application/pdf")})
        response.raise_for_status()
        version_id = response.json()["version_id"]
        previous, deadline = "", time.monotonic() + 90
        while time.monotonic() < deadline:
            response = client.get(f"/api/kb/versions/{version_id}/status")
            response.raise_for_status()
            state = response.json()
            if state["status"] != previous:
                previous = state["status"]
                print("knowledge:", previous, flush=True)
            if previous == "failed":
                raise RuntimeError(state["error"])
            if previous == "ready":
                break
            time.sleep(0.5)
        else:
            raise TimeoutError("知识文件未在90秒内完成入库")
        response = client.get(f"/api/kb/versions/{version_id}")
        response.raise_for_status()
        version = response.json()
        chunk = version["chunks"][0]
        citation = client.get(f"/api/kb/chunks/{chunk['chunk_id']}").json()
        original = client.get(citation["file_url"])
        original.raise_for_status()
        assert citation["page_start"] == 1 and hashlib.sha256(original.content).digest() == hashlib.sha256(pdf).digest()
        report["knowledge"] = {"version_id": version_id, "parser": version["parser"], "page": 1,
                               "chunks": len(version["chunks"]), "original_hash_matches": True}
        response = client.post("/api/sessions", json={"title": "PDF调查与审批修复演示"})
        response.raise_for_status()
        session_id = response.json()["id"]
        events, final, first_token_s, node_completed_s = [], None, None, None
        with httpx.Client(base_url=settings.logs_url, timeout=5, trust_env=False) as demo:
            response = demo.post("/control", json={"error_every": 1, "error_code": "ERR_DB_POOL_TIMEOUT"})
            response.raise_for_status()
            try:
                for _ in range(3):
                    demo.get("/work")
                started = time.monotonic()
                question = ("调查order-service最近5分钟的异常，查询同一窗口日志和指标，并检索《OpsPilot演练修复手册》。"
                            "区分测试注入与真实数据库根因，引用手册，说明证据缺口和审批后可执行的演练建议。")
                with client.stream("POST", "/api/agent/chat/stream", json={"session_id": session_id, "message": question}) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        event = json.loads(line[5:])
                        events.append(event)
                        if event["type"] == "token" and first_token_s is None:
                            first_token_s = round(time.monotonic() - started, 3)
                        if event["type"] == "node_status" and event.get("node") == "native_tools":
                            node_completed_s = round(time.monotonic() - started, 3)
                        if event["type"] in ("tool_start", "tool", "error"):
                            print("investigation:", event["type"], event.get("name", event.get("message", "")), flush=True)
                        if event["type"] == "error":
                            raise RuntimeError(event.get("message", "调查失败"))
                        if event["type"] == "final":
                            final = event
                assert final and final["answer"] and first_token_s is not None
                assert node_completed_s is not None and first_token_s <= node_completed_s
                run = client.get(f"/api/agent/runs/{final['run_id']}").json()
                config = run["data"]["config"]
                assert config["llm_provider"] == "openai" and config["embedding_provider"] == "openai" and config["agent_mode"] == "native"
                report["investigation"] = {"run_id": final["run_id"], "first_token_s": first_token_s,
                                           "node_completed_s": node_completed_s, "end_reason": final.get("end_reason"),
                                           "citations": final.get("citations", []), "backends": run["data"]["backends"],
                                           "tools": [e.get("name") for e in events if e["type"] == "tool"]}
                response = client.post("/api/remediations", json={"run_id": final["run_id"], "reason": "完整演示：审批后关闭测试注入并验证请求"})
                response.raise_for_status()
                repair_id = response.json()["id"]
                response = client.post(f"/api/remediations/{repair_id}/decide", json={"approved": True, "comment": "批准本次测试演练"})
                response.raise_for_status()
                repair = response.json()
                assert repair["status"] == "succeeded", repair
                report["remediation"] = {"id": repair_id, "status": repair["status"], "result": repair["result"]}
            finally:
                demo.post("/control", json={"error_every": 0, "delay_ms": 0})
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evaluation_record(report, question, final, run), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


def evaluation_record(report, question, final, run):
    calls = [e["detail"] for e in run["data"]["events"] if e["kind"] == "model_call"]
    evidence = final.get("hits") or [hit for event in run["data"]["events"] if event["kind"] == "tool"
                                     and event["detail"].get("agent", "main") == "main"
                                     for hit in event["detail"].get("result", {}).get("hits", [])]
    usage, agents = {"total_tokens": 0, "unknown_usage_calls": 0}, {}
    for call in calls:
        tokens = call.get("usage", {}).get("total_tokens")
        agent = agents.setdefault(call.get("agent", "main"), {"calls": 0, "total_tokens": 0, "unknown_usage_calls": 0})
        agent["calls"] += 1
        if isinstance(tokens, int):
            usage["total_tokens"] += tokens
            agent["total_tokens"] += tokens
        else:
            usage["unknown_usage_calls"] += 1
            agent["unknown_usage_calls"] += 1
    return {"batch": "demo_acceptance_20261111", "config": run["data"]["config"],
            "summary": {"cases": 1, "scope": "已见开发演示，程序验收通过；不是独立根因准确率"},
            "questions": {"demo-e2e": {"status": "completed", "question": question, "answer": final["answer"],
                                       "elapsed_s": report["investigation"]["node_completed_s"], "usage": usage,
                                       "agent_usage": agents, "actual_models": sorted({c["model"] for c in calls}),
                                       "citations": final.get("citations", []), "evidence": evidence,
                                       "program_checks": {"pdf_original_hash_matches": report["knowledge"]["original_hash_matches"],
                                                          "live_tokens_before_node_completion": True,
                                                          "approved_demo_remediation_succeeded": report["remediation"]["status"] == "succeeded"}}},
            "acceptance": report}


if __name__ == "__main__":
    main()

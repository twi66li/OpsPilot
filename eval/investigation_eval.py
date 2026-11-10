"""python -m eval.investigation_eval：实际测试请求与程序契约检查，不代表根因准确率。"""
import argparse
import json
import subprocess
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx

from app.config import get_settings


def scenarios():
    return [
        {"id": "dev-downstream", "split": "dev", "code": "ERR_DOWNSTREAM_TIMEOUT", "window": 5,
         "question": "调查order-service最近5分钟的错误，请查询当前指标、实际日志和适用手册，区分测试事实、假设及缺少的证据。", "tools": ["query_service_metrics", "query_logs", "search_runbook"], "forbid": []},
        {"id": "dev-pool", "split": "dev", "code": "ERR_DB_POOL_TIMEOUT", "window": 5,
         "question": "order-service最近5分钟返回500，请结合当前指标、实际日志和连接池手册排查，不能直接确认生产根因。", "tools": ["query_service_metrics", "query_logs", "search_runbook"], "forbid": []},
        {"id": "dev-logs", "split": "dev", "code": "ERR_INJECTED", "window": 5,
         "question": "只查order-service最近5分钟实际日志，说明发现的错误；不要查询指标、手册和历史故障。", "tools": ["query_logs"], "forbid": ["query_service_metrics", "search_runbook", "query_fault_history"]},
        {"id": "test-window", "split": "test", "code": "ERR_DOWNSTREAM_TIMEOUT", "window": 10,
         "question": "调查order-service最近10分钟的错误，对齐指标与实际日志时间窗口，查询适用手册，说明证据和未确认原因。", "tools": ["query_service_metrics", "query_logs", "search_runbook"], "forbid": []},
        {"id": "test-logs", "split": "test", "code": "ERR_DB_POOL_TIMEOUT", "window": 10,
         "question": "只查order-service最近10分钟实际日志中的连接获取超时证据，不要查询指标和历史故障。", "tools": ["query_logs"], "forbid": ["query_service_metrics", "query_fault_history"]},
        {"id": "test-source", "split": "test", "code": "ERR_INJECTED", "window": 5,
         "question": "order-service最近5分钟有500错误，请联合查询当前指标和实际日志，明确数据来源及缺少的证据。", "tools": ["query_service_metrics", "query_logs"], "forbid": []},
    ]


def grade_case(case, answer, trace, request_ids):
    tools = [e["detail"] for e in trace["data"]["events"] if e["kind"] == "tool" and e["detail"].get("executed")]
    names = {e["name"] for e in tools}
    rows = [row for e in tools if e["name"] == "query_logs" for row in e["result"].get("records", [])]
    timed = [e for e in tools if e["name"] in ("query_logs", "query_service_metrics")]
    config = trace["data"].get("config", {})
    return {
        "answered": answer.get("status") == "answered", "required_tools": set(case["tools"]) <= names,
        "forbidden_tools": not (set(case["forbid"]) & names),
        "actual_request_evidence": any(r.get("request_id") in request_ids and r.get("error_code") == case["code"] for r in rows),
        "requested_window": bool(timed) and all(e["args"].get("window_minutes") == case["window"] for e in timed),
        "aligned_window": len({json.dumps(e["result"].get("window"), sort_keys=True) for e in timed}) <= 1,
        "sources_available": bool(timed) and all(e["result"].get("status") == "ok" for e in timed),
        "metric_error_observed": "query_service_metrics" not in case["tools"] or any(
            (e["result"].get("avg", {}).get("error_rate_pct") or 0) > 0
            for e in timed if e["name"] == "query_service_metrics"
        ),
        "source_disclosed": any(word in answer.get("answer", "") for word in ("测试", "注入")),
        "mcp_used": not config.get("mcp_enabled") or bool(timed) and all(e["result"].get("mcp", {}).get("transport") == "stdio" for e in timed),
        "retrieval_subagent_used": not (config.get("retrieval_subagent_enabled") and "search_runbook" in case["tools"]) or any(
            e["name"] == "delegate_retrieval" and e["result"].get("status") == "ok" for e in tools),
    }


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--demo-url", default="http://127.0.0.1:18081")
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--case", choices=[case["id"] for case in scenarios()], help="只运行指定案例，避免重复整批")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path("eval/night20261110") / datetime.now().strftime("investigation_%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    cases = [case for case in scenarios() if not args.case or case["id"] == args.case][:args.limit]
    settings = get_settings()
    config = {"evaluation_scope": "测试服务实际请求＋程序契约检查；未人工复核，不是根因准确率或线上收益。",
              "generation": settings.chat_model, "embedding": settings.embedding_model, "rerank": settings.rerank_model,
              "judges": [], "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
    config.update(mcp_enabled=settings.mcp_enabled, retrieval_subagent_enabled=settings.retrieval_subagent_enabled)
    record = {"batch": output.name, "config": config, "questions": {}, "judges": {}, "summary": {"target": len(cases)}}
    save(output / "scenarios_frozen.json", {"scenarios": cases})
    with httpx.Client(timeout=180) as client:
        auth = client.post(args.base_url + "/auth/login", json={"username": settings.admin_username, "password": settings.admin_password})
        auth.raise_for_status()
        headers = {"Authorization": "Bearer " + auth.json()["access_token"]}
        try:
            for case in cases:
                started = time.perf_counter()
                row = {"question": case["question"], "split": case["split"], "family": case["code"], "model_config": config}
                try:
                    client.post(args.demo_url + "/control", json={"error_every": 2, "error_code": case["code"]}).raise_for_status()
                    requests = [client.get(args.demo_url + "/work") for _ in range(4)]
                    ids = {r.headers["X-Request-ID"] for r in requests if r.status_code == 500}
                    if "query_service_metrics" in case["tools"]:
                        time.sleep(3)  # 本地 Prometheus 每2秒抓取，调查快照需晚于注入请求的采集。
                    session = client.post(args.base_url + "/api/sessions", headers=headers, json={"title": case["id"]})
                    session.raise_for_status()
                    response = client.post(args.base_url + "/api/agent/chat", headers=headers,
                                           json={"session_id": session.json()["id"], "message": case["question"]})
                    response.raise_for_status()
                    answer = response.json()
                    response = client.get(args.base_url + "/api/agent/runs/" + answer["run_id"], headers=headers)
                    response.raise_for_status()
                    trace, usage, agent_usage = response.json(), {}, {}
                    checks = grade_case(case, answer, trace, ids)
                    tool_evidence = [e["detail"] for e in trace["data"]["events"] if e["kind"] == "tool" and e["detail"].get("executed")]
                    for event in trace["data"]["events"]:
                        if event["kind"] == "model_call":
                            detail = event["detail"]
                            item = usage.setdefault(detail.get("model", "unknown"), {"calls": 0, "tokens": 0, "missing_usage": 0})
                            total = (detail.get("usage") or {}).get("total_tokens")
                            item["calls"] += 1
                            item["tokens"] += total or 0
                            item["missing_usage"] += int(total is None)
                            agent = agent_usage.setdefault(detail.get("agent", "main"), {"calls": 0, "tokens": 0, "missing_usage": 0})
                            agent["calls"] += 1
                            agent["tokens"] += total or 0
                            agent["missing_usage"] += int(total is None)
                    row.update(status=answer["status"], answer=answer["answer"], program_checks=checks, usage=usage,
                               citations=answer.get("citations", []), actual_models=list(usage),
                               end_reason=answer.get("end_reason"), tool_evidence=tool_evidence,
                               agent_usage=agent_usage,
                               source_sha256=trace["data"].get("source_sha256"), passed=all(checks.values()))
                    save(output / f"{case['id']}_trace.json", trace)
                except Exception as exc:
                    row.update(status="failed", error=type(exc).__name__, passed=False)
                row["elapsed_s"] = round(time.perf_counter() - started, 3)
                record["questions"][case["id"]] = row
                record["summary"].update(completed=len(record["questions"]), program_passed=sum(r["passed"] for r in record["questions"].values()),
                                         status_counts=dict(Counter(r["status"] for r in record["questions"].values())))
                save(output / "results.json", record)
                print(json.dumps({"case": case["id"], "passed": row["passed"], "checks": row.get("program_checks"), "error": row.get("error")}, ensure_ascii=False), flush=True)
        finally:
            client.post(args.demo_url + "/control", json={}).raise_for_status()
    print(str(output.resolve()), flush=True)


if __name__ == "__main__":
    main()

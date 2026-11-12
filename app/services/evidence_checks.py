"""仅统计用户提交的样本；不请求网站、不将日志缺失视为恢复。"""
import json
import re
from datetime import datetime

_TIME = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)")
_NGINX_TIME = re.compile(r"\[(\d\d/[A-Za-z]{3}/\d{4}:\d\d:\d\d:\d\d [+-]\d{4})\]")
_REQUEST = re.compile(r'\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/[^\s"?]*)(?:\?[^\s"]*)?\s+HTTP(?:/\d(?:\.\d)?)?["\s]+([1-5]\d\d)\b', re.I)
_STATUS = re.compile(r"\bHTTP/\d(?:\.\d)?\s+([1-5]\d\d)\b", re.I)
_ELAPSED = re.compile(r"(?:elapsed|duration|耗时)\s*[=:：]\s*([\d.]+)\s*(ms|s)\b", re.I)
_POOL = re.compile(r"QueuePool|too many connections|connection pool.*(?:timeout|exhaust)|ERR_DB_POOL_TIMEOUT", re.I)
_ERROR_RECORD = re.compile(r"\b(?:ERROR|CRITICAL|FATAL|TimeoutError|OperationalError|ERR_DB_POOL_TIMEOUT)\b|^\s*(?:QueuePool|too many connections)", re.I)


def parse_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def measure(evidence):
    requests, errors, states = [], [], []
    for number, line in enumerate(evidence["text"].splitlines(), 1):
        timestamp = _TIME.search(line)
        at = timestamp[0] if timestamp else ""
        nginx_time = _NGINX_TIME.search(line)
        if not at and nginx_time:
            try:
                at = datetime.strptime(nginx_time[1], "%d/%b/%Y:%H:%M:%S %z").isoformat()
            except ValueError:
                pass
        request, status, duration = _REQUEST.search(line), _STATUS.search(line), _ELAPSED.search(line)
        if request or status:
            requests.append({"line": number, "method": request[1].upper() if request else "",
                             "endpoint": request[2] if request else evidence.get("endpoint", ""),
                             "status": int(request[3] if request else status[1]),
                             "at": at,
                             "elapsed_ms": float(duration[1]) * (1000 if duration[2].lower() == "s" else 1) if duration else None})
        if _POOL.search(line) and _ERROR_RECORD.search(line):
            errors.append(number)
        if "{" in line and any(key in line for key in ('"Status"', '"State"', '"OOMKilled"')):
            try:
                obj = json.loads(line[line.index("{"):])
                state = obj.get("State", obj)
                if isinstance(state, dict):
                    states.append({"line": number, **{k: state[k] for k in ("Status", "ExitCode", "OOMKilled", "Running") if k in state}})
            except (ValueError, TypeError):
                pass
    return {"evidence_id": evidence["id"], "kind": evidence["kind"], "request_count": len(requests),
            "failed_requests": sum(r["status"] >= 400 for r in requests), "requests": requests,
            "pool_error_lines": errors, "container_states": states}


def analyze_samples(context):
    evidence = context.get("evidence", [])
    measurements = [measure(e) for e in evidence]
    plan = context.get("plan", {})
    before_ids = set(plan.get("evidence_ids", []))
    fresh_ids = {e["id"] for e in evidence if e["kind"] == "check_result" and e["id"] not in before_ids
                 and e.get("plan_revision", plan.get("revision")) == plan.get("revision")}
    fresh = [m for m in measurements if m["evidence_id"] in fresh_ids]
    verification = {"verdict": "inconclusive", "evidence_ids": [m["evidence_id"] for m in fresh],
                    "reason": "请先确认检查方案，再提交包含接口、时间戳和响应状态的新结果。", "method": "program"}
    result = {"scope": "仅限用户提交样本，非实时监控", "measurements": measurements, "verification": verification}
    if not plan or not fresh:
        return result
    if any(m["pool_error_lines"] or any(s.get("OOMKilled") is True or s.get("Status") in ("exited", "dead", "restarting")
                                       for s in m["container_states"]) for m in fresh):
        verification.update(verdict="still_failing", reason="新结果仍包含连接池异常或容器异常状态；不能确认原问题已消失。")
        return result
    before = [r for m in measurements if m["evidence_id"] in before_ids for r in m["requests"] if r["status"] >= 400]
    targets = {(r["method"], r["endpoint"]) for r in before if r["endpoint"]}
    endpoint = context.get("problem", {}).get("endpoint", "")
    if endpoint:
        targets = {target for target in targets if target[1] == endpoint}
    if len(targets) != 1:
        verification["reason"] = "缺少唯一的原失败接口，或样本涉及多个接口；请补充问题接口和处理前的失败请求。"
        return result
    target = next(iter(targets))
    before = [r for r in before if (r["method"], r["endpoint"]) == target]
    relevant = [(m, r) for m in fresh for r in m["requests"] if r["endpoint"] == target[1] and r["method"] in ("", target[0])]
    if not relevant:
        verification["reason"] = "新结果未包含原失败接口的请求，容器running或其他接口成功不能证明原问题改善。"
        return result
    times_before = [time for r in before if (time := parse_time(r["at"]))]
    times_after = [time for _, r in relevant if (time := parse_time(r["at"]))]
    if not times_before or len(times_before) != len(before) or len(times_after) != len(relevant) or min(times_after) <= max(times_before):
        verification["reason"] = "请求时间缺失或新样本时间没有晚于原失败记录；请提交带时区的新请求时间戳。"
        return result
    failures = sum(r["status"] >= 400 for _, r in relevant)
    verification["evidence_ids"] = list(dict.fromkeys(m["evidence_id"] for m, _ in relevant))
    result["comparison"] = {"method": target[0], "endpoint": target[1], "before_failed_samples": len(before),
                            "after_requests": len(relevant), "after_failures": failures,
                            "after_from": min(times_after).isoformat(), "after_until": max(times_after).isoformat()}
    if failures:
        verification.update(verdict="still_failing", reason=f"原接口的新样本共{len(relevant)}次请求，仍有{failures}次失败。")
    elif any(not 200 <= r["status"] < 300 for _, r in relevant):
        verification["reason"] = "新样本含重定向等非2xx响应，请确认原业务接口的预期行为；暂不按成功请求核对。"
    elif len(relevant) < 3:
        verification.update(verdict="improved", reason=f"原接口已有{len(relevant)}次成功样本，请再补充请求结果；目前不足3次。")
    else:
        verification.update(verdict="sample_recovered", reason=f"原接口处理后的{len(relevant)}次提交样本均未返回HTTP错误，且时间晚于原失败记录；仅支持样本内原HTTP异常不再复现，不能证明生产长期恢复或连接泄漏已修复。")
    return result

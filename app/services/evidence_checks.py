"""仅统计用户提交的样本；不请求网站、不将日志缺失视为恢复。"""
import json
import re
from collections import defaultdict
from datetime import datetime
from hashlib import sha256

from app.services.scenario_rules import match_scenarios

_TIME = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)")
_NGINX_TIME = re.compile(r"\[(\d\d/[A-Za-z]{3}/\d{4}:\d\d:\d\d:\d\d [+-]\d{4})\]")
_REQUEST = re.compile(r'\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/[^\s"?]*)(?:\?[^\s"]*)?\s+HTTP(?:/\d(?:\.\d)?)?["\s]+([1-5]\d\d)\b', re.I)
_STATUS = re.compile(r"\bHTTP/\d(?:\.\d)?\s+([1-5]\d\d)\b", re.I)
_REQUEST_ID = re.compile(r'\b(?:x[-_])?request[-_]?id["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_.-]+)', re.I)
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
    for number, line in enumerate((evidence.get("text") or "").splitlines(), 1):
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
            request_id = _REQUEST_ID.search(line)
            identity = f"id:{request_id[1]}" if request_id else f"line:{line.strip()}"
            requests.append({"line": number, "method": request[1].upper() if request else "",
                             "endpoint": request[2] if request else evidence.get("endpoint", ""),
                             "status": int(request[3] if request else status[1]),
                             "at": at,
                             "sample_key": sha256(identity.encode()).hexdigest(),
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
    return {"evidence_id": evidence["id"], "kind": evidence.get("kind", "initial"), "request_count": len(requests),
            "failed_requests": sum(r["status"] >= 400 for r in requests), "requests": requests,
            "pool_error_lines": errors, "container_states": states}


def unique_requests(requests):
    """同一请求保留一个计数。状态冲突时，保留失败或非 2xx 的记录。"""
    distinct = {}
    for request in requests:
        key = request["sample_key"]
        previous = distinct.get(key)
        if previous is None or request["status"] // 100 > previous["status"] // 100:
            distinct[key] = request
    return list(distinct.values())


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
    public_measurements = [{**m, "requests": [{k: v for k, v in r.items() if k != "sample_key"} for r in m["requests"]]}
                           for m in measurements]
    scope = ("仅限已记录的巡检及提交样本" if any(e.get("source_kind") == "scheduled_http_probe" for e in evidence)
             else "仅限用户提交样本，非实时监控")
    result = {"scope": scope, "measurements": public_measurements, "verification": verification}
    if not plan or not fresh:
        return result
    if any(m["pool_error_lines"] or any(s.get("OOMKilled") is True or s.get("Status") in ("exited", "dead", "restarting")
                                       for s in m["container_states"]) for m in fresh):
        verification.update(verdict="still_failing", reason="新结果仍包含连接池异常或容器异常状态；不能确认原问题已消失。")
        return result
    before = [r for m in measurements if m["evidence_id"] in before_ids for r in m["requests"]]
    targets = {(r["method"], r["endpoint"]) for r in before if r["endpoint"] and r["status"] >= 400}
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
    before_unique = unique_requests(before)
    after_unique = unique_requests(r for _, r in relevant)
    failures = sum(r["status"] >= 400 for r in after_unique)
    duplicates = len(relevant) - len(after_unique)
    verification["evidence_ids"] = list(dict.fromkeys(m["evidence_id"] for m, _ in relevant))
    result["comparison"] = {"method": target[0], "endpoint": target[1],
                            "before_requests": len(before_unique),
                            "before_failed_samples": sum(r["status"] >= 400 for r in before_unique),
                            "before_from": min(times_before).isoformat(), "before_until": max(times_before).isoformat(),
                            "after_requests": len(after_unique), "after_failures": failures, "after_duplicates": duplicates,
                            "after_from": min(times_after).isoformat(), "after_until": max(times_after).isoformat()}
    if failures:
        verification.update(verdict="still_failing", reason=f"原接口的新样本共{len(after_unique)}次请求，仍有{failures}次失败。")
    elif any(not 200 <= r["status"] < 300 for _, r in relevant):
        verification["reason"] = "新样本含重定向等非2xx响应，请确认原业务接口的预期行为；暂不按成功请求核对。"
    elif len(after_unique) < 3:
        verification.update(verdict="improved", reason=f"原接口已有{len(after_unique)}次成功样本，请再补充请求结果；目前不足3次。")
    else:
        verification.update(verdict="sample_recovered", reason=f"原接口处理后的{len(after_unique)}次提交样本均未返回HTTP错误，且时间晚于处理前记录；仅支持样本内原HTTP异常不再复现，不能证明生产长期恢复或连接泄漏已修复。")
    return result


def summarize_evidence(evidence):
    """仅提取已提交/预览文本中的线索及原始行号，不生成诊断结论。"""
    measured = measure(evidence)
    text = evidence['text']
    lines = text.splitlines()
    timestamps, exceptions, repeated = [], defaultdict(list), defaultdict(list)
    for number, line in enumerate(lines, 1):
        timestamp = _TIME.search(line)
        parsed = parse_time(timestamp[0]) if timestamp else None
        nginx = _NGINX_TIME.search(line)
        if parsed is None and nginx:
            try:
                parsed = datetime.strptime(nginx[1], '%d/%b/%Y:%H:%M:%S %z')
            except ValueError:
                pass
        if parsed:
            timestamps.append(parsed)
        for name in set(re.findall(r'\b(?:[a-zA-Z_]\w*\.)*([A-Z]\w*(?:Error|Exception))\b', line)):
            exceptions[name].append(number)
        normalized = _NGINX_TIME.sub('', _TIME.sub('', line)).strip()
        if normalized:
            repeated[normalized].append(number)
    statuses = defaultdict(list)
    endpoints = defaultdict(list)
    for request in measured['requests']:
        statuses[request['status']].append(request['line'])
        endpoints[(request['method'], request['endpoint'])].append(request)
    return {
        'total_lines': len(lines), 'request_count': measured['request_count'],
        'failed_requests': measured['failed_requests'],
        'time_range': {'from': min(timestamps).isoformat(), 'until': max(timestamps).isoformat(),
                       'recognized': len(timestamps)} if timestamps else None,
        'http_statuses': [{'status': status, 'count': len(found), 'lines': found} for status, found in sorted(statuses.items())],
        'endpoints': [{'method': method, 'endpoint': endpoint, 'count': len(found),
                       'failures': sum(request['status'] >= 400 for request in found),
                       'lines': [request['line'] for request in found]} for (method, endpoint), found in endpoints.items()],
        'exceptions': [{'name': name, 'count': len(found), 'lines': found} for name, found in exceptions.items()],
        'repeated': [{'count': len(found), 'lines': found} for found in repeated.values() if len(found) > 1][:10],
        'scenarios': match_scenarios(text),
        'notice': '仅统计这份文本中可识别的记录；缺失时间或未识别内容不代表正常，不是实时流量或根因判断。',
    }


def compact_evidence(evidence):
    """同类异常保留代表行和出现次数，原文仍可按实际行号读取。"""
    lines = evidence["text"].splitlines()
    summary = summarize_evidence(evidence)
    groups = [
        {"label": entry["name"], "count": entry["count"], "line": entry["lines"][0]}
        for entry in sorted(summary["exceptions"], key=lambda entry: -entry["count"])[:6]
    ]
    groups.extend({"label": signal["label"], "count": signal["count"], "line": signal["lines"][0]}
                  for scenario in summary["scenarios"] for signal in scenario["signals"])
    groups.extend({"label": f"HTTP {entry['status']}", "count": entry["count"], "line": entry["lines"][0]}
                  for entry in summary["http_statuses"] if entry["status"] >= 400)
    if len(lines) <= 20:
        selected = range(1, len(lines) + 1)
    else:
        selected = sorted({1, 2, len(lines) - 1, len(lines), *(group["line"] for group in groups[:12])})
    visible = {number for number in selected if len(lines[number - 1]) <= 500}
    return {
        "id": evidence["id"], "label": evidence["label"], "kind": evidence["kind"],
        "source_kind": evidence.get("source_kind", "submitted"),
        "line_count": len(lines), "time_range": summary["time_range"], "groups": groups[:12],
        "lines": [{"line": number, "text": lines[number - 1]} for number in sorted(visible)],
    }, visible

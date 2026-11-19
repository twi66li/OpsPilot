"""复跑固定个人排障样本，比较全文与摘要加定向查证的证据契约和输入成本。"""

import json
from hashlib import sha256
from time import perf_counter

from app.services.evidence_checks import compact_evidence
from app.services.observability import estimate_tokens
from app.services.support_service import SupportReport, validate_report

ERROR = "2026-11-18T01:00:00Z GET /orders HTTP/1.1 502 TimeoutError"
COUNTER = "2026-11-18T01:00:01Z GET /orders HTTP/1.1 200"
SUCCESS = "2026-11-18T01:30:00Z GET /orders HTTP/1.1 200"
FROZEN_CASES = (
    ("contradicting-evidence", [ERROR] * 40 + [COUNTER] + [ERROR] * 40, [], (1, 41), "inconclusive"),
    ("missing-evidence", [ERROR], [], (1, 99), "inconclusive"),
    ("duplicate-retest", [ERROR] * 35, [SUCCESS] * 3, (1,), "improved"),
)


def compare_case(case):
    case_id, before, after, requested, expected_verdict = case
    evidence = [{"id": "before", "label": "故障时日志", "kind": "initial", "text": "\n".join(before)}]
    if after:
        evidence.append({"id": "after", "label": "复测日志", "kind": "check_result", "text": "\n".join(after)})
    context = {"evidence": evidence, "plan": {"evidence_ids": ["before"]} if after else {}}

    refs = [{"evidence_id": "before", "line_start": line, "line_end": line} for line in requested]
    prediction = SupportReport.model_validate({
        "summary": "结算接口存在间歇性错误，原因仍需核对。",
        "hypotheses": [{"cause": "结算接口持续返回错误", "supporting": refs[:1],
                        "contradicting": refs[1:], "missing": ["持续观测的请求记录"]}],
    })
    result = {"case": case_id, "input_sha256": sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()}
    for strategy in ("full", "compact"):
        started = perf_counter()
        if strategy == "full":
            payload = [{**item, "text": "\n".join(f"L{i}: {line}" for i, line in enumerate(item["text"].splitlines(), 1))}
                       for item in evidence]
            visible = None
        else:
            payload, visible = [], {}
            for item in evidence:
                overview, visible[item["id"]] = compact_evidence(item)
                payload.append(overview)
            # 固定定向查询模拟同一模型回答所需的原文行，计入摘要方案的输入。
            follow_up = [{"line": line, "text": before[line - 1]} for line in requested
                         if line <= len(before) and line not in visible["before"]]
            visible["before"].update(item["line"] for item in follow_up)
            if follow_up:
                payload.append({"evidence_id": "before", "targeted_lines": follow_up})
        report = validate_report(prediction, context, [], visible=visible)
        hypothesis = report["hypotheses"][0]
        kept = [span["line_start"] for span in hypothesis["supporting"] + hypothesis["contradicting"]]
        result[strategy] = {
            "verdict": report["verification"]["verdict"], "retained_lines": kept,
            "elapsed_ms": round((perf_counter() - started) * 1000, 3),
            "estimated_evidence_tokens": estimate_tokens(json.dumps(payload, ensure_ascii=False)),
            "passed": report["verification"]["verdict"] == expected_verdict and kept == [n for n in requested if n <= len(before)],
        }
    return result


if __name__ == "__main__":
    rows = [compare_case(case) for case in FROZEN_CASES]
    print(json.dumps({"scope": "固定样本与固定候选输出。耗时为本地处理，Token 为证据输入估算。", "cases": rows},
                     indent=2, ensure_ascii=False))
    raise SystemExit(int(not all(row[name]["passed"] for row in rows for name in ("full", "compact"))))

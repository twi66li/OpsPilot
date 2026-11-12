"""对照共同输入的程序检查，不将完成率冒充答案准确率。"""

from collections import Counter


def checks(value, prefix=""):
    result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(item, bool):
                result[path] = item
            elif isinstance(item, dict):
                result.update(checks(item, path))
    return result


def compare_reports(baseline, candidate):
    before = {q.get("question", "").strip(): q for q in baseline["questions"] if q.get("question")}
    after = {q.get("question", "").strip(): q for q in candidate["questions"] if q.get("question")}
    repeated = {
        text for text in before if sum(q.get("question", "").strip() == text for q in baseline["questions"]) > 1
    }
    repeated |= {
        text for text in after if sum(q.get("question", "").strip() == text for q in candidate["questions"]) > 1
    }
    pairs = []
    clusters = Counter()
    regressions = 0
    comparable_checks = 0
    for question in sorted(before.keys() & after.keys() - repeated):
        old, new = before[question], after[question]
        a, b = checks(old.get("program_checks")), checks(new.get("program_checks"))
        shared = a.keys() & b.keys()
        comparable_checks += len(shared)
        worse = [key for key in shared if a[key] and not b[key]]
        better = [key for key in shared if not a[key] and b[key]]
        regressions += len(worse)
        pairs.append(
            {
                "question": question,
                "baseline_id": old["id"],
                "candidate_id": new["id"],
                "regressions": worse,
                "improvements": better,
                "missing_checks": sorted(a.keys() - b.keys()),
                "baseline_elapsed_s": old.get("elapsed_s"),
                "candidate_elapsed_s": new.get("elapsed_s"),
                "baseline_tokens": old.get("usage", {}).get("total_tokens"),
                "candidate_tokens": new.get("usage", {}).get("total_tokens"),
            }
        )
    for item in candidate["questions"]:
        reason = item.get("end_reason")
        if reason and reason != "answered":
            clusters[str(reason)] += 1
        elif item.get("error") or item.get("status") in ("failed", "error"):
            clusters["执行失败"] += 1
        elif any(v is False for v in checks(item.get("program_checks")).values()):
            clusters["程序检查未通过"] += 1
        elif not item.get("program_checks") and not item.get("judges"):
            clusters["缺少质量判分"] += 1
    missing = sum(bool(p["missing_checks"]) for p in pairs)
    gate = (
        "needs_review" if regressions or missing else "no_observed_regression" if comparable_checks else "inconclusive"
    )
    return {
        "baseline": baseline["id"],
        "candidate": candidate["id"],
        "common_questions": len(pairs),
        "comparable_checks": comparable_checks,
        "regressions": regressions,
        "missing_check_questions": missing,
        "gate": gate,
        "pairs": pairs,
        "failure_clusters": [{"reason": reason, "count": count} for reason, count in clusters.items()],
        "scope": "只比较文本完全相同、无重复歧义的问题和共有程序检查。数据、模型及环境可能不同；不能据此认定独立准确率或单项优化收益。",
    }

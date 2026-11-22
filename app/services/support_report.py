"""排障报告模型、恢复结论核对、文本渲染与案例序列化。"""

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.services.case_service import case_status, is_archived
from app.services.evidence_checks import analyze_samples, summarize_evidence


class EvidenceSpan(BaseModel):
    evidence_id: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class Observation(EvidenceSpan):
    meaning: str = Field(max_length=350)


class Hypothesis(BaseModel):
    cause: str = Field(min_length=1, max_length=200)
    supporting: list[EvidenceSpan] = Field(default_factory=list, max_length=3)
    contradicting: list[EvidenceSpan] = Field(default_factory=list, max_length=3)
    missing: list[str] = Field(default_factory=list, max_length=3)


class CheckStep(BaseModel):
    location: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=800)
    normal: str = Field(min_length=1, max_length=300)
    abnormal: str = Field(min_length=1, max_length=300)
    risk: Literal["只读", "会修改系统"]
    rollback: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def require_rollback(self):
        if self.risk == "会修改系统" and not self.rollback.strip():
            raise ValueError("修改操作需要说明风险和回退方法")
        return self


class Verification(BaseModel):
    verdict: Literal["inconclusive", "still_failing", "improved", "sample_recovered"] = "inconclusive"
    evidence_ids: list[str] = Field(default_factory=list, max_length=5)
    reason: str = Field(default="尚未提供处理后的检查结果", max_length=500)


class SupportReport(BaseModel):
    summary: str = Field(min_length=1, max_length=500)
    observations: list[Observation] = Field(default_factory=list, max_length=5)
    hypotheses: list[Hypothesis | str] = Field(default_factory=list, max_length=3)
    missing_info: list[str] = Field(default_factory=list)
    steps: list[CheckStep] = Field(default_factory=list, max_length=3)
    references: list[int] = Field(default_factory=list, max_length=8)
    verification: Verification = Field(default_factory=Verification)


_RECOVERY_CLAIM = re.compile(
    r"(?:已(?:经)?|确认|判定|证明)(?:全部|完全|全面|成功)?(?:恢复|修复|解决|排除)"
    r"|(?:故障|异常|错误)(?:已(?:经)?|均已)?(?:消失|解除)"
    r"|(?:恢复正常|恢复完成|运行正常|服务正常|业务正常|故障解除|恢复了|恢复成功|修复成功)"
)


def ground_recovery_claims(data, verification, workflow, *, model_claim=False):
    data["verification"] = verification
    if workflow in ("diagnose", "review"):
        if (
            workflow == "review"
            or _RECOVERY_CLAIM.search(data["summary"])
            or (model_claim and verification["verdict"] != "sample_recovered")
        ):
            data["summary"] = verification["reason"]
        for observation in data["observations"]:
            if _RECOVERY_CLAIM.search(observation["meaning"]):
                observation["meaning"] = "此处为提交的原始记录，恢复状态以程序核对结果为准。"
    return data


def validate_report(report, context, hits, workflow="diagnose", *, visible=None):
    evidence = {item["id"]: item["text"].splitlines() for item in context.get("evidence", [])}

    def verified_span(span):
        lines = evidence.get(span.evidence_id, [])
        if not 1 <= span.line_start <= span.line_end <= len(lines):
            return None
        if visible is not None and not set(range(span.line_start, span.line_end + 1)) <= visible.get(
            span.evidence_id, set()
        ):
            return None
        return {**span.model_dump(), "quote": "\n".join(lines[span.line_start - 1 : span.line_end])}

    selected = {h["citation_id"] for h in hits}
    data = report.model_dump()
    data["observations"] = [value for obs in report.observations if (value := verified_span(obs))]
    data["hypotheses"] = [
        hypothesis
        if isinstance(hypothesis, str)
        else {
            **hypothesis.model_dump(),
            "supporting": [value for span in hypothesis.supporting if (value := verified_span(span))],
            "contradicting": [value for span in hypothesis.contradicting if (value := verified_span(span))],
        }
        for hypothesis in report.hypotheses
    ]
    data["references"] = [n for n in report.references if n in selected]
    checks = analyze_samples(context)
    return ground_recovery_claims(
        data,
        checks["verification"],
        workflow,
        model_claim=report.verification.verdict == "sample_recovered",
    )


def render_report(report):
    if report.get("workflow") in ("consult", "history", "welcome"):
        refs = " ".join(f"[{n}]" for n in report["references"])
        return report["summary"] + ("\n\n资料引用：" + refs if refs else "")
    out = [report["summary"], "\n已观察到的证据"]
    for obs in report["observations"]:
        out.append(f"{obs['evidence_id']} L{obs['line_start']}–L{obs['line_end']}：{obs['meaning']}\n{obs['quote']}")
    if not report["observations"]:
        out.append("尚无可核对的日志行，当前只能提供基础指导。")
    if report["hypotheses"]:
        out.append("\n待验证的可能原因")
        for hypothesis in report["hypotheses"]:
            if isinstance(hypothesis, str):
                out.append(f"• {hypothesis}")
                continue
            out.append(f"• {hypothesis['cause']}")
            for label, key in (("支持证据", "supporting"), ("反对证据", "contradicting")):
                spans = [f"{span['evidence_id']} L{span['line_start']}–L{span['line_end']}" for span in hypothesis[key]]
                out.append(f"  {label}：{'、'.join(spans) or '尚无'}")
            if hypothesis["missing"]:
                out.append("  待补充：" + "、".join(hypothesis["missing"]))
    if report["missing_info"]:
        out.extend(["\n需要补充的信息", *[f"• {item}" for item in report["missing_info"]]])
    for i, step in enumerate(report["steps"], 1):
        out.extend(
            [
                f"\n检查 {i} · {step['risk']} · {step['location']}",
                step["action"],
                "正常：" + step["normal"],
                "异常：" + step["abnormal"],
            ]
        )
        if step["rollback"]:
            out.append("影响与回退：" + step["rollback"])
    out.extend(["\n结果核对", report["verification"]["reason"]])
    if report["references"]:
        out.append("资料依据：" + " ".join(f"[{n}]" for n in report["references"]))
    return "\n".join(out)


def report_output(report, hits, events):
    answer = render_report(report)
    citations = [h["doc_title"] for h in hits]
    return {
        "answer": answer,
        "support_report": report,
        "hits": hits,
        "end_reason": "answered",
        "citations": list(dict.fromkeys(citations)),
        "citations_indexed": citations,
        "events": events + [{"type": "answer", "answer": answer, "support_report": report, "hits": hits}],
    }


def case_dict(case):
    sample_checks = analyze_samples(case.data)
    verification = sample_checks["verification"]
    report = case.data.get("report")
    if report and report.get("workflow", "diagnose") in ("diagnose", "review"):
        report = ground_recovery_claims(
            {**report, "observations": [dict(item) for item in report.get("observations", [])]},
            verification,
            report.get("workflow", "diagnose"),
            model_claim=report.get("verification", {}).get("verdict") == "sample_recovered",
        )
    status = case_status(case)
    if status == "sample_recovered" and verification["verdict"] != "sample_recovered":
        status = "awaiting_results" if case.data.get("plan") else "investigating"
    return {
        **case.data,
        "report": report,
        "evidence": [{**item, "summary": summarize_evidence(item)} for item in case.data.get("evidence", [])],
        "id": case.session_id,
        "site_id": case.site_id,
        "status": status,
        "archived": is_archived(case),
        "saved": is_archived(case),
        "kind": case.data.get("kind", "problem"),
        "sample_checks": sample_checks,
        "severity": case.severity,
        "labels": case.labels,
        "updated_at": case.updated_at.isoformat(),
        "created_at": case.created_at.isoformat(),
    }

"""只读日志采集指引与案例 Markdown 报告导出。"""

import re
import shlex

from app.services.case_service import case_status
from app.services.scenario_rules import BY_COLLECTION_KEY


def collection_guide(scenario, deployment, target):
    if scenario not in BY_COLLECTION_KEY:
        raise ValueError("未知异常场景")
    if target and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,99}", target):
        raise ValueError("容器或服务名只能包含字母、数字、点、下划线、@和短横线")
    if deployment == "unknown":
        return [
            {
                "label": "先补充部署方式",
                "command": "",
                "purpose": "确认应用使用Docker、systemd还是其他方式启动；不确定时说明你平时如何启动网站。",
            }
        ]
    if not target:
        return (
            [
                {
                    "label": "列出容器",
                    "command": "docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'",
                    "purpose": "找出应用或Nginx的实际容器名，然后再生成日志命令",
                }
            ]
            if deployment == "docker"
            else [
                {
                    "label": "确认服务名",
                    "command": "systemctl list-units --type=service --no-pager",
                    "purpose": "找到实际应用服务名，再生成对应日志命令",
                }
            ]
        )
    name = shlex.quote(target)
    commands = (
        [
            {
                "label": "应用日志",
                "command": f"docker logs --timestamps --since 10m --tail 200 {name}",
                "purpose": "保留报错时间和异常前后文；Nginx场景请选择网关容器",
            },
            {
                "label": "容器状态",
                "command": f"docker inspect --format '{{{{json .State}}}}' {name}",
                "purpose": "检查退出码、OOM和健康状态；仅running不能证明业务恢复",
            },
        ]
        if deployment == "docker"
        else [
            {
                "label": "服务日志",
                "command": f"journalctl -u {name} --since '10 minutes ago' -n 200 --no-pager",
                "purpose": "保留服务日志与时间戳；无权限时联系管理员执行",
            },
            {
                "label": "服务状态",
                "command": f"systemctl status --no-pager -- {name}",
                "purpose": "查看进程状态和最近退出原因",
            },
        ]
    )
    explanation = BY_COLLECTION_KEY[scenario]["collection_notes"]
    return [
        {**c, "scenario_notes": explanation, "risk": "只读采集；平台不会执行命令。提交结果前请脱敏。"} for c in commands
    ]


def case_report(case, title, events):
    data = case.data
    report = data.get("report") or {}
    state_labels = {
        "investigating": "排查中",
        "needs_information": "待补充信息",
        "awaiting_results": "待提交检查结果",
        "evidence_submitted": "已补充结果",
        "sample_recovered": "提交样本支持症状不再复现",
        "user_resolved": "用户确认解决",
    }
    event_labels = {
        "site_change": "网站变更（手工记录）",
        "evidence_observed": "日志样本时间",
        "case_created": "登记问题",
        "problem_updated": "补充问题信息",
        "status_changed": "更新处理状态",
        "evidence_submitted": "提交证据",
        "plan_confirmed": "确认检查方案",
        "step_updated": "记录检查结果",
        "analysis_completed": "生成分析报告",
        "comment": "补充说明",
        "metadata_changed": "更新分类",
        "case_archived": "归档记录",
        "handoff_assigned": "交接给成员复核",
        "handoff_reviewed": "提交复核意见",
        "handoff_revoked": "撤回交接",
    }
    lines = [
        f"# {title}",
        "",
        f"状态：{state_labels.get(case_status(case), case_status(case))}；严重度：{case.severity}",
        f"创建时间（UTC）：{case.created_at.isoformat()}",
        "",
        "## 网站环境",
        "",
    ]
    profile = data.get("profile", {})
    for key, label in (
        ("name", "网站"),
        ("url", "网址"),
        ("environment", "运行环境"),
        ("group", "业务分组"),
        ("owner", "负责人"),
        ("framework", "框架"),
        ("deployment", "部署方式"),
        ("database", "数据库"),
        ("versions", "版本"),
        ("notes", "维护说明"),
    ):
        if profile.get(key):
            lines.append(f"- {label}：{profile[key]}")
    if not profile:
        lines.append("尚未提供网站背景")
    lines.extend(
        [
            "",
            "## 问题与当前判断",
            "",
            data.get("problem", {}).get("symptom", "未记录异常表现"),
            report.get("summary", "尚无有效报告"),
            "",
            "## 提交的证据",
            "",
        ]
    )
    for evidence in data.get("evidence", []):
        fence = "`" * max(4, max((len(x) for x in re.findall(r"`+", evidence["text"])), default=0) + 1)
        lines.extend(
            [
                f"### {evidence['id']} · {evidence['label']}",
                f"来源：用户提交；采集时间：{evidence.get('collected_at') or '未说明'}；提交时间：{evidence.get('submitted_at', '未知')}",
                fence + "text",
                evidence["text"],
                fence,
                "",
            ]
        )
    plan = data.get("plan", {})
    lines.extend(
        ["## 检查方案与用户记录", "", f"已确认方案版本：{plan['revision']}" if plan else "以下建议尚未由用户确认。"]
    )
    for index, step in enumerate(plan.get("steps", report.get("steps", []))):
        lines.extend(
            [
                f"### 检查{index + 1} · {step.get('location', '未指定位置')}",
                step.get("action", ""),
                "风险：" + step.get("risk", "未说明"),
                "正常情况：" + step.get("normal", "未说明"),
                "异常情况：" + step.get("abnormal", "未说明"),
            ]
        )
        if step.get("rollback"):
            lines.append("影响与回退：" + step["rollback"])
        checks = plan.get("checks", [])
        if index < len(checks):
            lines.extend(
                [
                    "用户记录："
                    + {"done": "已执行（不代表恢复）", "pending": "待执行", "skipped": "暂未执行"}.get(
                        checks[index]["status"], checks[index]["status"]
                    ),
                    checks[index].get("note", ""),
                ]
            )
    lines.extend(["", "## 资料依据", ""])
    references = set(report.get("references", []))
    for hit in data.get("report_hits", []):
        if hit.get("citation_id") in references:
            lines.append(
                f"- [{hit.get('citation_id', '?')}] {hit.get('doc_title', '')} · 版本 {hit.get('version', '整理稿')} · {hit.get('source', '')}"
            )
    lines.extend(["", "## 用户处理结局", "", data.get("solution", "尚未确认实际结局"), "", "## 维护时间线", ""])
    for event in events:
        detail = event["detail"]
        description = " · ".join(
            str(detail[key])
            for key in ("actor", "title", "version", "label", "text", "note", "rollback", "solution", "severity")
            if detail.get(key)
        )
        if detail.get("status"):
            description += " · " + state_labels.get(detail["status"], detail["status"])
        lines.append(f"- {event['at']} · {event_labels.get(event['kind'], '维护活动')} · {description}")
    verification = report.get("verification", {})
    lines.extend(
        [
            "",
            "## 验证结果与边界",
            "",
            verification.get("reason", "尚无可核对的处理后样本"),
            "本报告基于提交样本和引用资料，不代表已接入生产监控，也不能证明持续恢复。",
        ]
    )
    return "\n".join(lines)

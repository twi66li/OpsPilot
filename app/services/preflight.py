"""统一变更前预检（Preflight）：覆盖工单审批与演练修复，输出结构化风险评估与门禁结论。

设计原则：
- 硬违规（权限不足、目标非法、草稿关键字段缺失、动作未在白名单）始终阻断（passed=False）；
- 软风险（单人自提自批、缺少回滚/验证说明）在默认演示模式记为 warn（人工可见），
  在开启 strict_four_eyes=True 时升级为 fail 强制双人复核（四眼原则）。
"""

from app.config import get_settings
from app.services import remediation_service

_ALLOWED_ROLES = {"admin", "engineer"}
_ALLOWED_SEVERITIES = {"P1", "P2", "P3"}


def _item(key: str, label: str, status: str, detail: str) -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail}


def _summarize(checks: list[dict], *, risk_level: str, target: str, rollback_hint: str) -> dict:
    failed = [c for c in checks if c["status"] == "fail"]
    warned = [c for c in checks if c["status"] == "warn"]
    return {
        "passed": len(failed) == 0,
        "risk_level": risk_level,
        "target": target,
        "rollback_hint": rollback_hint,
        "summary": (
            f"预检未通过（{len(failed)} 项阻断）"
            if failed
            else f"预检通过（含 {len(warned)} 项风险提示）"
            if warned
            else "预检全部通过"
        ),
        "checks": checks,
    }


def evaluate_ticket_preflight(
    draft: dict | None,
    *,
    requested_by: str,
    approver_username: str = "",
    approver_role: str = "",
    strict_four_eyes: bool = False,
) -> dict:
    draft = draft or {}
    title = str(draft.get("title") or "").strip()
    severity = str(draft.get("severity") or "P2").upper()
    description = str(draft.get("description") or "").strip()

    checks = [
        _item(
            "draft_completeness",
            "工单草稿完整性",
            "pass" if title and severity in _ALLOWED_SEVERITIES else "fail",
            f"标题已填写，级别 {severity}" if title and severity in _ALLOWED_SEVERITIES else "缺少标题或严重级别不在 P1–P3 范围内",
        ),
        _item(
            "evidence_context",
            "排障上下文说明",
            "pass" if len(description) >= 8 else "warn",
            "包含故障背景与处理摘要" if len(description) >= 8 else "描述较短，建议审批人核对原对话证据",
        ),
    ]

    if approver_role:
        checks.append(
            _item(
                "approver_role",
                "审批人角色权限",
                "pass" if approver_role in _ALLOWED_ROLES else "fail",
                f"当前角色 {approver_role} 具备审批权限" if approver_role in _ALLOWED_ROLES else f"角色 {approver_role} 无权审批",
            )
        )

    if approver_username:
        same_actor = bool(requested_by) and requested_by == approver_username
        if same_actor:
            checks.append(
                _item(
                    "four_eyes",
                    "双人复核（四眼原则）",
                    "fail" if strict_four_eyes else "warn",
                    "申请人与审批人为同一账号，严格模式下禁止自提自批"
                    if strict_four_eyes
                    else "申请人与审批人为同一账号（单人演示允许，生产建议启用双人复核）",
                )
            )
        else:
            checks.append(_item("four_eyes", "双人复核（四眼原则）", "pass", f"申请人 {requested_by or '系统'} 与审批人 {approver_username} 分离"))

    risk_level = "high" if severity == "P1" else "medium" if severity == "P2" else "low"
    return _summarize(
        checks,
        risk_level=risk_level,
        target="工单系统（创建跟踪单，不改动线上运行态）",
        rollback_hint="工单创建后可在工单中心关闭或补充处理结论。",
    )


def evaluate_remediation_preflight(
    action: str,
    *,
    service: str,
    tenant_id: str,
    requested_by: str,
    approver_username: str = "",
    approver_role: str = "",
    remediation_enabled: bool = True,
    strict_four_eyes: bool = False,
) -> dict:
    target_service = get_settings().drill_service
    allowed_target = tenant_id == "default" and service == target_service
    checks = [
        _item(
            "feature_gate",
            "演练修复开关",
            "pass" if remediation_enabled else "fail",
            "当前环境已启用演练修复通道" if remediation_enabled else "REMEDIATION_ENABLED 未启用",
        ),
        _item(
            "action_whitelist",
            "动作白名单校验",
            "pass" if action == remediation_service.ACTION else "fail",
            f"固定受控动作：{remediation_service.action_label(service)}"
            if action == remediation_service.ACTION
            else f"未知或未授权动作：{action}",
        ),
        _item(
            "target_scope",
            "目标环境与租户边界",
            "pass" if allowed_target else "fail",
            f"限定测试租户 default / {target_service} 演练靶场，不触及个人生产网站"
            if allowed_target
            else "目标租户或服务不在演练白名单内",
        ),
    ]

    if approver_role:
        checks.append(
            _item(
                "approver_role",
                "审批人角色权限",
                "pass" if approver_role in _ALLOWED_ROLES else "fail",
                f"当前角色 {approver_role} 具备审批权限" if approver_role in _ALLOWED_ROLES else f"角色 {approver_role} 无权审批",
            )
        )

    if approver_username:
        same_actor = bool(requested_by) and requested_by == approver_username
        if same_actor:
            checks.append(
                _item(
                    "four_eyes",
                    "双人复核（四眼原则）",
                    "fail" if strict_four_eyes else "warn",
                    "申请人与审批人为同一账号，严格模式下禁止自提自批"
                    if strict_four_eyes
                    else "申请人与审批人为同一账号（单人演示允许，生产建议启用双人复核）",
                )
            )
        else:
            checks.append(_item("four_eyes", "双人复核（四眼原则）", "pass", f"申请人 {requested_by} 与审批人 {approver_username} 分离"))

    return _summarize(
        checks,
        risk_level="medium",
        target=f"测试演练服务 {service}（复位故障注入并执行前后各 3 次 HTTP 探针验证）",
        rollback_hint="如验证未通过，不自动重试；请检查测试服务 /control 状态后重新演练。",
    )

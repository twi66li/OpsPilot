"""声明式 Skill 注册表：将业务工作流与工具边界、预算、证据契约集中声明。

实际可用工具始终由服务器侧取交集决定：
    effective_tools = Skill.allowed_tools ∩ runtime_allowed ∩ RBAC/租户隔离
模型不能越过 Skill 声明的工具白名单或调用写操作。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillSpec:
    name: str
    label: str
    description: str
    allowed_tools: tuple[str, ...]
    max_tool_calls: int
    evidence_policy: str
    write_allowed: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "allowed_tools": list(self.allowed_tools),
            "max_tool_calls": self.max_tool_calls,
            "evidence_policy": self.evidence_policy,
            "write_allowed": self.write_allowed,
        }


SKILLS: dict[str, SkillSpec] = {
    "consult": SkillSpec(
        name="consult",
        label="维护知识咨询",
        description="基于官方运维资料、个人绑定手册与网站档案回答架构/配置/排障概念问题，不虚构现场日志。",
        allowed_tools=("get_site_overview", "search_runbook"),
        max_tool_calls=3,
        evidence_policy="只引用已命中的官方资料或本租户授权手册；不给出恢复结论。",
    ),
    "diagnose": SkillSpec(
        name="diagnose",
        label="证据驱动排障",
        description="结合用户脱敏提交的日志片段、网站背景、变更时间线与手册给出分层假设、只读检查与下一步建议。",
        allowed_tools=("get_site_overview", "search_runbook", "search_saved_cases", "read_evidence_lines"),
        max_tool_calls=3,
        evidence_policy="只允许引用实际可见日志行号与手册编号；恢复结论完全交由程序样本核对。",
    ),
    "review": SkillSpec(
        name="review",
        label="复测结果核对",
        description="对比确认方案前后新提交的复测样本，结合程序统计结果判断是否仍存在异常或仅限样本改善。",
        allowed_tools=("get_site_overview", "search_runbook", "read_evidence_lines"),
        max_tool_calls=3,
        evidence_policy="禁止仅因日志缺失宣布恢复；结论以 analyze_samples 程序核对为准。",
    ),
    "history": SkillSpec(
        name="history",
        label="历史案例检索",
        description="在当前网站的已归档/已确认处理案例中检索相似症状与处理经验，明确区分历史参考与当前根因。",
        allowed_tools=("get_site_overview", "search_saved_cases"),
        max_tool_calls=3,
        evidence_policy="仅检索本人同一网站的历史案例；不把历史案例当作当前已确认根因。",
    ),
    "drill": SkillSpec(
        name="drill",
        label="独立故障演练调查",
        description="面向平台内置测试服务的原生工具调查，联合查询指标、请求日志、历史故障与运维手册。",
        allowed_tools=(
            "search_runbook",
            "query_service_metrics",
            "query_fault_history",
            "query_logs",
            "delegate_retrieval",
        ),
        max_tool_calls=5,
        evidence_policy="强制携带时间窗、指标来源与阈值判定；写操作（建单/演练复位）必须人工审批。",
    ),
}


def resolve_skill(name: str) -> SkillSpec:
    return SKILLS.get(name, SKILLS["consult"])


def effective_tools(skill_name: str, *, runtime_allowed: tuple[str, ...] | list[str] | None = None) -> tuple[str, ...]:
    """返回 Skill 声明与运行时允许列表的交集（保留声明顺序）。"""
    spec = resolve_skill(skill_name)
    if runtime_allowed is None:
        return spec.allowed_tools
    allowed_set = set(runtime_allowed)
    return tuple(tool for tool in spec.allowed_tools if tool in allowed_set)


def list_skills() -> list[dict]:
    return [spec.to_dict() for spec in SKILLS.values()]

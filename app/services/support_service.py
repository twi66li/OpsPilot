"""个人排障服务门面：按职责拆分至证据脱敏、报告核对、混合检索与诊断编排子模块，保留原有导入兼容。"""

from app.services.case_service import is_archived
from app.services.support_diagnosis import SYSTEM, choose_workflow, run_personal
from app.services.support_evidence import (
    CaseIn,
    DependencyIn,
    EvidenceIn,
    SiteIn,
    context_for_session,
    new_evidence,
    owned_case,
    owned_site,
    redact,
    validate_private_docs,
)
from app.services.support_report import (
    CheckStep,
    EvidenceSpan,
    Hypothesis,
    Observation,
    SupportReport,
    Verification,
    case_dict,
    ground_recovery_claims,
    render_report,
    report_output,
    validate_report,
)
from app.services.support_retrieval import (
    get_site_overview,
    read_evidence_lines,
    retrieve_for_case,
    search_saved_cases,
)

__all__ = [
    "CaseIn",
    "CheckStep",
    "DependencyIn",
    "EvidenceIn",
    "EvidenceSpan",
    "Hypothesis",
    "Observation",
    "SYSTEM",
    "SiteIn",
    "SupportReport",
    "Verification",
    "case_dict",
    "choose_workflow",
    "context_for_session",
    "get_site_overview",
    "ground_recovery_claims",
    "is_archived",
    "new_evidence",
    "owned_case",
    "owned_site",
    "read_evidence_lines",
    "redact",
    "render_report",
    "report_output",
    "retrieve_for_case",
    "run_personal",
    "search_saved_cases",
    "validate_private_docs",
    "validate_report",
]

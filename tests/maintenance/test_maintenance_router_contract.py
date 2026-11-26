"""T67 模块拆分验收：OpenAPI 契约、公共导入兼容与跨子路由权限边界。"""

from app.api import (
    maintenance_cases,
    maintenance_handoff,
    maintenance_sites,
    maintenance_workspace,
    router_maintenance,
    router_support,
    support_cases,
    support_evidence,
    support_library,
    support_sites,
)
from app.main import app
from app.services import (
    handoff_service,
    maintenance_report,
    maintenance_service,
    maintenance_timeline,
    support_diagnosis,
    support_report,
    support_retrieval,
    support_service,
)
from app.services import maintenance_workspace as maintenance_workspace_service
from app.services import support_evidence as support_evidence_service
from tests.conftest import register

EXPECTED_MAINTENANCE_OPERATIONS = {
    ("/api/maintenance/activity", "get"): "recent_activity_api_maintenance_activity_get",
    ("/api/maintenance/capabilities", "get"): "capabilities_api_maintenance_capabilities_get",
    ("/api/maintenance/cases/{case_id}/comments", "post"): "comment_api_maintenance_cases__case_id__comments_post",
    ("/api/maintenance/cases/{case_id}/handoff", "delete"): "revoke_handoff_api_maintenance_cases__case_id__handoff_delete",
    ("/api/maintenance/cases/{case_id}/handoff", "put"): "assign_handoff_api_maintenance_cases__case_id__handoff_put",
    ("/api/maintenance/cases/{case_id}/metadata", "put"): "metadata_api_maintenance_cases__case_id__metadata_put",
    ("/api/maintenance/cases/{case_id}/report", "get"): "export_report_api_maintenance_cases__case_id__report_get",
    ("/api/maintenance/cases/{case_id}/timeline", "get"): "case_timeline_api_maintenance_cases__case_id__timeline_get",
    ("/api/maintenance/collection-guide", "post"): "guide_api_maintenance_collection_guide_post",
    ("/api/maintenance/documents/retry-failed", "post"): "retry_failed_api_maintenance_documents_retry_failed_post",
    ("/api/maintenance/evidence/file-preview", "post"): "preview_file_api_maintenance_evidence_file_preview_post",
    ("/api/maintenance/handoffs", "get"): "received_handoffs_api_maintenance_handoffs_get",
    ("/api/maintenance/handoffs/{case_id}/review", "post"): "review_handoff_api_maintenance_handoffs__case_id__review_post",
    ("/api/maintenance/operations/{record_id}", "get"): "operation_detail_api_maintenance_operations__record_id__get",
    ("/api/maintenance/retrieval-test", "post"): "retrieval_test_api_maintenance_retrieval_test_post",
    ("/api/maintenance/sites/{site_id}/changes", "post"): "add_change_api_maintenance_sites__site_id__changes_post",
    ("/api/maintenance/sites/{site_id}/http-check", "post"): "http_check_api_maintenance_sites__site_id__http_check_post",
    ("/api/maintenance/sites/{site_id}/http-monitor", "put"): "configure_http_monitor_api_maintenance_sites__site_id__http_monitor_put",
    ("/api/maintenance/sites/{site_id}/timeline", "get"): "site_timeline_api_maintenance_sites__site_id__timeline_get",
    ("/api/maintenance/sites/{site_id}/webhook", "post"): "receive_site_webhook_api_maintenance_sites__site_id__webhook_post",
    ("/api/maintenance/sites/{site_id}/webhook-token", "delete"): "revoke_site_webhook_token_api_maintenance_sites__site_id__webhook_token_delete",
    ("/api/maintenance/sites/{site_id}/webhook-token", "post"): "rotate_site_webhook_token_api_maintenance_sites__site_id__webhook_token_post",
    ("/api/maintenance/todos", "get"): "todos_api_maintenance_todos_get",
}

EXPECTED_SUPPORT_OPERATIONS = {
    ("/api/support/cases", "get"): "cases_api_support_cases_get",
    ("/api/support/cases", "post"): "create_case_api_support_cases_post",
    ("/api/support/cases/{session_id}", "delete"): "delete_case_api_support_cases__session_id__delete",
    ("/api/support/cases/{session_id}", "get"): "get_case_api_support_cases__session_id__get",
    ("/api/support/cases/{session_id}/confirm-plan", "post"): "confirm_plan_api_support_cases__session_id__confirm_plan_post",
    ("/api/support/cases/{session_id}/evidence", "post"): "submit_evidence_api_support_cases__session_id__evidence_post",
    ("/api/support/cases/{session_id}/plan/steps/{step_index}", "post"): "update_step_api_support_cases__session_id__plan_steps__step_index__post",
    ("/api/support/cases/{session_id}/problem", "put"): "update_problem_api_support_cases__session_id__problem_put",
    ("/api/support/cases/{session_id}/save", "post"): "save_case_api_support_cases__session_id__save_post",
    ("/api/support/cases/{session_id}/status", "post"): "change_status_api_support_cases__session_id__status_post",
    ("/api/support/cases/{session_id}/track", "post"): "track_case_api_support_cases__session_id__track_post",
    ("/api/support/documents/upload", "post"): "upload_personal_api_support_documents_upload_post",
    ("/api/support/documents/{doc_id}", "delete"): "delete_personal_document_api_support_documents__doc_id__delete",
    ("/api/support/evidence/preview", "post"): "preview_api_support_evidence_preview_post",
    ("/api/support/library", "get"): "library_api_support_library_get",
    ("/api/support/library/{doc_id}", "get"): "library_document_api_support_library__doc_id__get",
    ("/api/support/private-documents", "get"): "private_documents_api_support_private_documents_get",
    ("/api/support/scenarios", "get"): "scenarios_api_support_scenarios_get",
    ("/api/support/sites", "get"): "sites_api_support_sites_get",
    ("/api/support/sites", "post"): "create_site_api_support_sites_post",
    ("/api/support/sites/{site_id}", "delete"): "delete_site_api_support_sites__site_id__delete",
    ("/api/support/sites/{site_id}", "get"): "site_detail_api_support_sites__site_id__get",
    ("/api/support/sites/{site_id}", "put"): "update_site_api_support_sites__site_id__put",
}


def test_openapi_contract_preserved_after_split():
    schema = app.openapi()
    paths = schema["paths"]

    actual_maint = {
        (path, method): spec["operationId"]
        for path, methods in paths.items()
        if path.startswith("/api/maintenance")
        for method, spec in methods.items()
    }
    assert actual_maint == EXPECTED_MAINTENANCE_OPERATIONS
    for path, method in EXPECTED_MAINTENANCE_OPERATIONS:
        assert paths[path][method]["tags"] == ["maintenance"]

    actual_support = {
        (path, method): spec["operationId"]
        for path, methods in paths.items()
        if path.startswith("/api/support")
        for method, spec in methods.items()
    }
    assert actual_support == EXPECTED_SUPPORT_OPERATIONS
    for path, method in EXPECTED_SUPPORT_OPERATIONS:
        assert paths[path][method]["tags"] == ["personal-support"]

    # 关键状态码、请求模型与 Header 参数零回归
    assert "201" in paths["/api/maintenance/sites/{site_id}/changes"]["post"]["responses"]
    assert "202" in paths["/api/support/documents/upload"]["post"]["responses"]
    webhook_op = paths["/api/maintenance/sites/{site_id}/webhook"]["post"]
    header_names = {p["name"] for p in webhook_op["parameters"] if p["in"] == "header"}
    assert {"x-opspilot-webhook-token", "authorization"} <= header_names

    component_schemas = set(schema["components"]["schemas"])
    expected_schemas = {
        "GuideIn",
        "ChangeIn",
        "MetadataIn",
        "CommentIn",
        "HttpMonitorIn",
        "HandoffIn",
        "HandoffReviewIn",
        "RetrievalIn",
        "RetryIn",
        "DependencyIn",
        "SiteIn",
        "CaseIn",
        "EvidenceIn",
        "ProblemIn",
        "StatusIn",
        "StepIn",
        "SaveIn",
    }
    assert expected_schemas <= component_schemas


def test_module_facades_export_compatible_symbols():
    # 聚合路由包含拆分后的全部子路由且保留辅助导出
    assert router_maintenance.router.prefix == "/api/maintenance"
    assert router_support.router.prefix == "/api/support"
    assert router_support.case_dict is support_report.case_dict
    assert router_support.save_site is support_sites.save_site
    assert all(
        len(sub.router.routes) > 0
        for sub in (
            maintenance_workspace,
            maintenance_sites,
            maintenance_cases,
            maintenance_handoff,
            support_library,
            support_sites,
            support_cases,
            support_evidence,
        )
    )

    # maintenance_service 与 support_service 保持单源导出，无重复定义分叉
    assert maintenance_service.record_event is maintenance_timeline.record_event
    assert maintenance_service.recent_site_changes is maintenance_timeline.recent_site_changes
    assert maintenance_service.collection_guide is maintenance_report.collection_guide
    assert maintenance_service.case_report is maintenance_report.case_report
    assert maintenance_service.site_summaries is maintenance_workspace_service.site_summaries
    assert maintenance_service.assign_case_handoff is handoff_service.assign_case_handoff

    assert support_service.redact is support_evidence_service.redact
    assert support_service.owned_case is support_evidence_service.owned_case
    assert support_service.owned_site is support_evidence_service.owned_site
    assert support_service.context_for_session is support_evidence_service.context_for_session
    assert support_service.SupportReport is support_report.SupportReport
    assert support_service.validate_report is support_report.validate_report
    assert support_service.retrieve_for_case is support_retrieval.retrieve_for_case
    assert support_service.choose_workflow is support_diagnosis.choose_workflow
    assert support_service.run_personal is support_diagnosis.run_personal
    assert callable(support_service.is_archived)


def test_split_router_permissions_and_isolation(client, admin_headers, viewer_headers):
    # 未登录访问维护与排障接口均被拒绝
    for path in (
        "/api/maintenance/capabilities",
        "/api/maintenance/handoffs",
        "/api/support/sites",
        "/api/support/cases",
    ):
        assert client.get(path).status_code == 401

    # Webhook 入口不依赖登录态，但令牌错误必须返回 401
    assert client.post("/api/maintenance/sites/nonexistent/webhook", json={"status": "firing"}).status_code == 401

    # viewer 不能访问组织审批/工单详情
    assert client.get("/api/maintenance/operations/nonexistent", headers=viewer_headers).status_code == 403

    # 跨用户隔离：不同用户无法读取或修改彼此的站点与排障案例
    owner_headers = register(client, "t67_owner", "owner123456")
    other_headers = register(client, "t67_other", "other123456")

    site_resp = client.post("/api/support/sites", json={"name": "隔离测试站点"}, headers=owner_headers)
    assert site_resp.status_code == 200
    site_id = site_resp.json()["id"]

    case_resp = client.post(
        "/api/support/cases",
        json={"title": "隔离测试案例", "site_id": site_id, "symptom": "502"},
        headers=owner_headers,
    )
    assert case_resp.status_code == 200
    case_id = case_resp.json()["id"]

    assert client.get(f"/api/support/sites/{site_id}", headers=other_headers).status_code == 404
    assert client.get(f"/api/maintenance/sites/{site_id}/timeline", headers=other_headers).status_code == 404
    assert client.get(f"/api/support/cases/{case_id}", headers=other_headers).status_code == 404
    assert client.get(f"/api/maintenance/cases/{case_id}/timeline", headers=other_headers).status_code == 404
    assert client.get(f"/api/maintenance/cases/{case_id}/report", headers=other_headers).status_code == 404
    assert (
        client.post(
            f"/api/support/cases/{case_id}/evidence",
            json={"text": "502 bad gateway", "label": "日志"},
            headers=other_headers,
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"/api/maintenance/cases/{case_id}/handoff",
            json={"recipient": "t67_owner", "note": "尝试越权交接"},
            headers=other_headers,
        ).status_code
        == 404
    )

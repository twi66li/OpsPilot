"""查询测试服务实际请求日志；身份由服务端注入，原文保留供证据核查。"""
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.services.run_service import tool_time_left


def query_logs(tenant_id: str, service: str, window_minutes: int, limit: int = 30, error_code: str | None = None, *, evaluation_time: float) -> dict:
    start = evaluation_time - window_minutes * 60
    result = {
        "status": "unavailable", "service": service,
        "source": {"kind": "http_request_logs", "environment": "test", "faults": "测试注入"},
        "window": {"start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                   "end": datetime.fromtimestamp(evaluation_time, timezone.utc).isoformat()},
        "records": [],
    }
    params = {"tenant_id": tenant_id, "service": service, "start": start, "end": evaluation_time, "limit": limit}
    if error_code:
        params["error_code"] = error_code
    try:
        remaining = tool_time_left()
        with httpx.Client(timeout=min(3.0, remaining) if remaining is not None else 3.0) as client:
            response = client.get(get_settings().logs_url.rstrip("/") + "/logs", params=params)
            response.raise_for_status()
            data = response.json()
        records = data["records"]
        if not isinstance(records, list) or any(
            row["tenant_id"] != tenant_id or row["service"] != service or not start <= row["timestamp"] <= evaluation_time
            for row in records
        ):
            raise ValueError("日志来源返回了查询范围之外的数据")
        return {**result, "status": "ok" if records else "no_data", "records": records,
                "matched": data["matched"], "truncated": data["matched"] > len(records),
                "summary": f"测试服务实际请求日志：命中{data['matched']}条，返回{len(records)}条；空结果不能确认服务正常。"}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return {**result, "error_type": type(exc).__name__, "summary": "日志源不可用或证据范围不匹配，不能确认根因。"}

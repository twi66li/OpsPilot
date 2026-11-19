"""按网站配置运行只读探测，把连续异常写入同一个问题。"""

import logging
from datetime import timedelta
from threading import Event, Thread
from urllib.parse import urlsplit, urlunsplit

from app.db.base import SessionLocal
from app.db.models import WebsiteMonitor, WebsiteProfile, _now, _uuid
from app.services.case_service import create_case_record
from app.services.http_probe import probe_url, validate_probe_url
from app.services.maintenance_service import record_event

logger = logging.getLogger(__name__)
FAILURE_THRESHOLD = 3
_stop = Event()
_worker = None


def probe_target(site):
    parts = urlsplit(site.data.get("url", ""))
    validate_probe_url(parts.geturl())
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


def monitor_info(monitor):
    if not monitor:
        return {"enabled": False, "interval_minutes": 15, "failure_threshold": FAILURE_THRESHOLD}
    return {
        "enabled": monitor.enabled, "interval_minutes": monitor.interval_minutes,
        "next_check_at": monitor.next_check_at.isoformat() + "Z" if monitor.enabled else None,
        "failure_threshold": FAILURE_THRESHOLD, "consecutive_failures": len(monitor.data.get("failures", [])),
        "last_result": monitor.data.get("last_result"), "case_id": monitor.data.get("case_id"),
    }


def configure(db, site, enabled, interval_minutes):
    target = probe_target(site) if enabled else None
    monitor = db.get(WebsiteMonitor, site.id, with_for_update=True)
    if monitor is None:
        monitor = WebsiteMonitor(site_id=site.id, data={})
        db.add(monitor)
    if enabled and target != monitor.data.get("target"):
        monitor.data = {"target": target}
    elif enabled != monitor.enabled:
        monitor.data = {**monitor.data, "failures": []}
    monitor.enabled, monitor.interval_minutes = enabled, interval_minutes
    monitor.next_check_at = _now()
    db.commit()
    return monitor_info(monitor)


def incident_evidence(failures, endpoint):
    rows = []
    for result in failures:
        if result.get("http_status"):
            rows.append(f"{result['collected_at']} GET {endpoint} HTTP/1.1 {result['http_status']}")
        else:
            rows.append(f"{result['collected_at']} GET {endpoint} 无响应（巡检请求超时）")
    return {
        "id": _uuid(), "kind": "initial", "label": "定时 HTTP 巡检",
        "source_kind": "scheduled_http_probe", "endpoint": endpoint,
        "text": "\n".join(rows), "collected_at": failures[0]["collected_at"],
        "submitted_at": _now().isoformat() + "Z",
    }


def save_result(db, site, monitor, target, result):
    data = dict(monitor.data) if monitor.data.get("target") == target else {"target": target}
    case_id = data.get("case_id")
    failures = data.get("failures", [])
    result = {**result, "source_kind": "scheduled_http_probe"}
    if target and (result["state"] == "abnormal" or result.get("timed_out")):
        failures = [*failures, result][-FAILURE_THRESHOLD:]
        if len(failures) == FAILURE_THRESHOLD and not case_id:
            endpoint = urlsplit(target).path
            symptom = f"巡检位置连续 {FAILURE_THRESHOLD} 次请求收到异常响应或超时。"
            case = create_case_record(
                db, site.tenant_id, site.user_id, f"{site.data['name']} · HTTP 巡检异常",
                {"symptom": symptom, "endpoint": endpoint, "started_at": failures[0]["collected_at"]},
                actor="定时巡检", site=site, evidence=[incident_evidence(failures, endpoint)],
            )
            case_id = case.session_id
            record_event(db, site.tenant_id, site.user_id, "site", site.id, "http_incident",
                         {"title": "定时 HTTP 巡检发现连续异常", "case_id": case_id})
    else:
        failures = []
        if result["state"] == "normal" and case_id:
            record_event(db, site.tenant_id, site.user_id, "site", site.id, "http_probe_normal",
                         {"title": "巡检收到正常响应，问题仍可人工复核", "case_id": case_id})
            case_id = None
    monitor.data = {**data, "last_result": result, "failures": failures, "case_id": case_id}
    monitor.next_check_at = _now() + timedelta(minutes=monitor.interval_minutes)
    db.commit()


def check_site(site_id, due_at):
    with SessionLocal() as db:
        site = db.get(WebsiteProfile, site_id)
        if not site or site.deleted_at:
            return
        saved_url = site.data.get("url")
        try:
            target = probe_target(site)
        except ValueError:
            target = None
    try:
        result = probe_url(target) if target else None
    except (ValueError, OSError):
        result = None
    if result is None:
        result = {"state": "unknown", "collected_at": _now().isoformat() + "Z",
                  "note": "巡检无法取得有效响应，请核对公开网址和 DNS。"}
    with SessionLocal() as db:
        site = db.get(WebsiteProfile, site_id)
        monitor = db.get(WebsiteMonitor, site_id, with_for_update=True)
        if site and not site.deleted_at and site.data.get("url") == saved_url and monitor and monitor.enabled and monitor.next_check_at == due_at:
            save_result(db, site, monitor, target, result)


def run_due_checks(stop=None):
    with SessionLocal() as db:
        due = (
            db.query(WebsiteMonitor.site_id, WebsiteMonitor.next_check_at)
            .join(WebsiteProfile, WebsiteProfile.id == WebsiteMonitor.site_id)
            .filter(WebsiteMonitor.enabled.is_(True), WebsiteMonitor.next_check_at <= _now(), WebsiteProfile.deleted_at.is_(None))
            .order_by(WebsiteMonitor.next_check_at, WebsiteMonitor.site_id).limit(12).all()
        )
    for site_id, due_at in due:
        if stop and stop.is_set():
            return
        check_site(site_id, due_at)


def start_worker():
    global _worker
    _stop.clear()

    def loop():
        while not _stop.wait(30):
            try:
                run_due_checks(_stop)
            except Exception as exc:
                logger.warning("website_monitor_failed error_type=%s", type(exc).__name__)

    _worker = Thread(target=loop, name="website-monitor", daemon=True)
    _worker.start()


def stop_worker():
    global _worker
    _stop.set()
    if _worker:
        _worker.join()
        _worker = None

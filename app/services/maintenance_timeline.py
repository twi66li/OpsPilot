"""维护事件记录、时间线聚合与近期活动。"""

from datetime import datetime, timezone

from app.db.models import ChatSession, SupportCase, WebsiteProfile, WorkspaceEvent
from app.services.evidence_checks import summarize_evidence


def _utc_event_time(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def event_at(occurred_at, recorded_at):
    stamp = _utc_event_time(occurred_at) or _utc_event_time(recorded_at)
    return stamp.isoformat().replace("+00:00", "Z")


def event_dict(row):
    occurred = row.detail.get("occurred_at") if row.kind == "site_change" else None
    at = occurred if (isinstance(occurred, str) and _utc_event_time(occurred) is not None) else event_at(None, row.created_at)
    return {
        "id": row.id,
        "kind": row.kind,
        "detail": row.detail,
        "at": at,
        "recorded_at": event_at(None, row.created_at),
        "case_id": row.resource_id if row.resource_type == "case" else row.detail.get("case_id"),
    }


def sort_events(events):
    def timestamp(event):
        return (
            _utc_event_time(event.get("at"))
            or _utc_event_time(event.get("recorded_at"))
            or datetime.max.replace(tzinfo=timezone.utc)
        )

    return sorted(events, key=timestamp)


def recent_site_changes(db, tenant_id, user_id, site_id):
    if not site_id:
        return []
    rows = (
        db.query(WorkspaceEvent)
        .filter_by(tenant_id=tenant_id, user_id=user_id, resource_type="site", resource_id=site_id, kind="site_change")
        .order_by(WorkspaceEvent.id.desc())
        .limit(200)
        .all()
    )
    return sort_events([event_dict(row) for row in rows])[-10:]


def record_event(db, tenant_id, user_id, resource_type, resource_id, kind, detail):
    db.add(
        WorkspaceEvent(
            tenant_id=tenant_id,
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            kind=kind,
            detail=detail,
        )
    )


def timeline(db, user, resource_type, resource_id):
    records = (
        db.query(WorkspaceEvent)
        .filter_by(tenant_id=user.tenant_id, user_id=user.user_id, resource_type=resource_type, resource_id=resource_id)
        .order_by(WorkspaceEvent.id)
        .all()
    )
    return [event_dict(r) for r in records]


def observed_events(case):
    events = []
    for evidence in case.data.get("evidence", []):
        if not evidence.get("text"):
            continue
        span = summarize_evidence(evidence)["time_range"]
        if span:
            events.append(
                {
                    "id": f'observed-{case.session_id}-{evidence["id"]}',
                    "kind": "evidence_observed",
                    "case_id": case.session_id,
                    "at": span["from"],
                    "detail": {
                        "label": evidence["label"],
                        "evidence_id": evidence["id"],
                        "until": span["until"],
                        "note": "定时 HTTP 巡检采样时间"
                        if evidence.get("source_kind") == "scheduled_http_probe"
                        else "已提交日志的样本时间",
                    },
                }
            )
    return events


def build_case_timeline(db, user, case):
    events = timeline(db, user, "case", case.session_id)
    events.extend(recent_site_changes(db, user.tenant_id, user.user_id, case.site_id))
    represented = {e["detail"].get("evidence_id") for e in events}
    for e in case.data.get("evidence", []):
        if e["id"] not in represented:
            events.append(
                {
                    "id": "legacy-" + e["id"],
                    "kind": "evidence_submitted",
                    "detail": {"evidence_id": e["id"], "label": e["label"]},
                    "at": event_at(e.get("submitted_at"), case.created_at),
                }
            )
    events.extend(observed_events(case))
    return {"events": sort_events(events), "site_id": case.site_id}


def build_site_timeline(db, user, site_id):
    cases = (
        db.query(SupportCase)
        .join(ChatSession)
        .filter(
            SupportCase.site_id == site_id,
            SupportCase.tenant_id == user.tenant_id,
            SupportCase.user_id == user.user_id,
            ChatSession.deleted_at.is_(None),
        )
    ).all()
    case_ids = [row.session_id for row in cases]
    events = (
        db.query(WorkspaceEvent)
        .filter(
            WorkspaceEvent.tenant_id == user.tenant_id,
            WorkspaceEvent.user_id == user.user_id,
            ((WorkspaceEvent.resource_type == "site") & (WorkspaceEvent.resource_id == site_id))
            | ((WorkspaceEvent.resource_type == "case") & (WorkspaceEvent.resource_id.in_(case_ids))),
        )
        .order_by(WorkspaceEvent.id.desc())
        .limit(200)
        .all()
    )
    combined = [event_dict(e) for e in events]
    combined.extend(event for case in cases for event in observed_events(case))
    return {"events": sort_events(combined), "site_id": site_id}


def list_recent_activity(db, user):
    sites = {
        site.id: site.data["name"]
        for site in db.query(WebsiteProfile)
        .filter_by(tenant_id=user.tenant_id, user_id=user.user_id, deleted_at=None)
        .all()
    }
    events = (
        db.query(WorkspaceEvent)
        .filter(
            WorkspaceEvent.tenant_id == user.tenant_id,
            WorkspaceEvent.user_id == user.user_id,
            WorkspaceEvent.resource_type == "site",
            WorkspaceEvent.resource_id.in_(sites),
            WorkspaceEvent.kind.in_(("site_change", "http_incident", "http_probe_normal", "webhook_alert")),
        )
        .all()
    )
    activity = [
        {**event_dict(event), "siteId": event.resource_id, "siteName": sites[event.resource_id]}
        for event in events
    ]
    cases = (
        db.query(SupportCase)
        .join(ChatSession)
        .filter(
            SupportCase.tenant_id == user.tenant_id,
            SupportCase.user_id == user.user_id,
            SupportCase.site_id.in_(sites),
            ChatSession.deleted_at.is_(None),
        )
        .all()
    )
    activity.extend(
        {**event, "at": event["detail"]["until"], "siteId": case.site_id, "siteName": sites[case.site_id]}
        for case in cases
        for event in observed_events(case)
    )
    return {"events": sort_events(activity)[-4:][::-1]}

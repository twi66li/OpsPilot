"""外部监控告警 Webhook 接入：支持 Alertmanager、Uptime Kuma 与通用 JSON 载荷。

安全边界：
- 每个网站独立生成 32 字节随机令牌，数据库仅保存 SHA-256 哈希与前 8 位前缀；
- 校验使用 hmac.compare_digest 常量时间比对，避免时序侧信道；
- 仅做只读接收、脱敏、聚合登记到对应网站问题与维护时间线，不执行任何自动写修复。
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from app.db.models import ChatSession, SupportCase, WebsiteProfile, _now, _uuid
from app.services.case_service import create_case_record
from app.services.maintenance_service import record_event
from app.services.support_service import is_archived, redact


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_site_data(data: dict) -> dict:
    """对外序列化网站资料时剥离 Webhook 令牌哈希，仅暴露只读摘要。"""
    cleaned = dict(data or {})
    webhook = cleaned.get("webhook")
    if isinstance(webhook, dict):
        cleaned["webhook"] = {
            "enabled": bool(webhook.get("token_sha256")),
            "token_prefix": webhook.get("token_prefix", ""),
            "created_at": webhook.get("created_at"),
            "received_count": int(webhook.get("received_count", 0)),
            "last_received_at": webhook.get("last_received_at"),
        }
    return cleaned


def rotate_token(db, site: WebsiteProfile) -> dict:
    token = "whk_" + secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc).isoformat()
    prev_count = int((site.data.get("webhook") or {}).get("received_count", 0))
    site.data = {
        **site.data,
        "webhook": {
            "token_sha256": _hash_token(token),
            "token_prefix": token[:12],
            "created_at": now,
            "received_count": prev_count,
            "last_received_at": (site.data.get("webhook") or {}).get("last_received_at"),
        },
    }
    db.commit()
    return {
        "enabled": True,
        "token": token,
        "token_prefix": token[:12],
        "created_at": now,
        "endpoint": f"/api/maintenance/sites/{site.id}/webhook",
    }


def revoke_token(db, site: WebsiteProfile) -> dict:
    data = dict(site.data or {})
    data.pop("webhook", None)
    site.data = data
    db.commit()
    return {"enabled": False}


def verify_token(site: WebsiteProfile, raw_token: str) -> bool:
    webhook = (site.data or {}).get("webhook")
    if not isinstance(webhook, dict) or not webhook.get("token_sha256") or not raw_token:
        return False
    return hmac.compare_digest(str(webhook["token_sha256"]), _hash_token(raw_token.strip()))


def parse_alert_payload(payload: dict) -> dict:
    """把 Alertmanager / Uptime Kuma / 通用 JSON 统一规整为结构化告警条目。"""
    now_iso = datetime.now(timezone.utc).isoformat()
    if isinstance(payload.get("alerts"), list) and payload["alerts"]:
        alerts = payload["alerts"][:10]
        first = alerts[0] if isinstance(alerts[0], dict) else {}
        labels = first.get("labels") if isinstance(first.get("labels"), dict) else {}
        annotations = first.get("annotations") if isinstance(first.get("annotations"), dict) else {}
        status = str(payload.get("status") or first.get("status") or "firing")
        title = str(
            annotations.get("summary")
            or labels.get("alertname")
            or payload.get("commonLabels", {}).get("alertname")
            or "Alertmanager 告警"
        )[:120]
        lines = []
        for item in alerts:
            if not isinstance(item, dict):
                continue
            al = item.get("labels") if isinstance(item.get("labels"), dict) else {}
            an = item.get("annotations") if isinstance(item.get("annotations"), dict) else {}
            at = str(item.get("startsAt") or now_iso)
            name = al.get("alertname", "alert")
            st = item.get("status", status)
            desc = an.get("summary") or an.get("description") or al.get("instance") or ""
            lines.append(f"{at} [{st.upper()}] {name} {desc}".strip())
        severity = _normalize_severity(labels.get("severity"))
        return {
            "format": "alertmanager",
            "status": status,
            "title": title,
            "severity": severity,
            "endpoint": str(labels.get("endpoint") or labels.get("instance") or "")[:120],
            "occurred_at": str(first.get("startsAt") or now_iso),
            "lines": "\n".join(lines)[:8000] or f"{now_iso} Alertmanager 告警",
        }

    if isinstance(payload.get("heartbeat"), dict) or isinstance(payload.get("monitor"), dict):
        hb = payload.get("heartbeat") if isinstance(payload.get("heartbeat"), dict) else {}
        mon = payload.get("monitor") if isinstance(payload.get("monitor"), dict) else {}
        hb_status = hb.get("status")
        status = "resolved" if hb_status == 1 else "firing"
        name = str(mon.get("name") or mon.get("url") or "Uptime Kuma 监控")[:100]
        msg = str(payload.get("msg") or hb.get("msg") or "状态变化")[:500]
        at = str(hb.get("time") or now_iso)
        return {
            "format": "uptime_kuma",
            "status": status,
            "title": f"{name} · {'恢复通知' if status == 'resolved' else '可用性告警'}"[:120],
            "severity": "P2" if status == "resolved" else "P1",
            "endpoint": str(mon.get("url") or "")[:120],
            "occurred_at": at,
            "lines": f"{at} [{status.upper()}] {name} {msg}"[:8000],
        }

    title = str(payload.get("title") or payload.get("alert") or payload.get("name") or "外部监控告警")[:120]
    summary = str(payload.get("summary") or payload.get("message") or payload.get("detail") or title)[:4000]
    status = str(payload.get("status") or "firing")[:32]
    at = str(payload.get("occurred_at") or payload.get("timestamp") or now_iso)[:64]
    endpoint = str(payload.get("endpoint") or payload.get("path") or "")[:120]
    severity = _normalize_severity(payload.get("severity"))
    return {
        "format": "generic",
        "status": status,
        "title": title,
        "severity": severity,
        "endpoint": endpoint,
        "occurred_at": at,
        "lines": f"{at} [{status.upper()}] {title}\n{summary}"[:8000],
    }


def _normalize_severity(value) -> str:
    text = str(value or "").strip().lower()
    if text in ("p0", "critical", "fatal", "emergency"):
        return "P0"
    if text in ("p1", "high", "error", "page"):
        return "P1"
    if text in ("p3", "low", "info", "notice"):
        return "P3"
    return "P2"


def ingest_webhook_alert(db, site: WebsiteProfile, payload: dict) -> dict:
    parsed = parse_alert_payload(payload)
    redacted_lines = redact(parsed["lines"])["text"]
    redacted_title = redact(parsed["title"])["text"]
    now_iso = _now().isoformat() + "Z"
    evidence_item = {
        "id": _uuid(),
        "kind": "initial",
        "label": f"Webhook 告警（{parsed['format']}）",
        "source_kind": "webhook_alert",
        "endpoint": parsed["endpoint"],
        "text": redacted_lines,
        "collected_at": parsed["occurred_at"],
        "submitted_at": now_iso,
    }

    # 聚合到该站点最近一条未归档的 Webhook 问题，避免告警风暴产生大量重复工单/问题
    existing = (
        db.query(SupportCase)
        .join(ChatSession, ChatSession.id == SupportCase.session_id)
        .filter(
            SupportCase.site_id == site.id,
            SupportCase.tenant_id == site.tenant_id,
            SupportCase.user_id == site.user_id,
            ChatSession.deleted_at.is_(None),
        )
        .order_by(SupportCase.updated_at.desc())
        .limit(10)
        .all()
    )
    target_case = next(
        (
            row
            for row in existing
            if not is_archived(row)
            and row.status != "user_resolved"
            and row.data.get("origin") == "webhook_alert"
        ),
        None,
    )
    if target_case is not None:
        evidence_list = [*target_case.data.get("evidence", []), evidence_item][-20:]
        target_case.data = {**target_case.data, "evidence": evidence_list}
        target_case.updated_at = _now()
        case_id = target_case.session_id
        created = False
    else:
        case = create_case_record(
            db,
            site.tenant_id,
            site.user_id,
            f"{site.data.get('name', '网站')} · {redacted_title}"[:120],
            {
                "symptom": redacted_title,
                "endpoint": parsed["endpoint"],
                "started_at": parsed["occurred_at"],
            },
            actor="Webhook 告警",
            site=site,
            evidence=[evidence_item],
        )
        case.severity = parsed["severity"]
        case.data = {**case.data, "origin": "webhook_alert"}
        case_id = case.session_id
        created = True

    webhook = dict(site.data.get("webhook") or {})
    webhook["received_count"] = int(webhook.get("received_count", 0)) + 1
    webhook["last_received_at"] = now_iso
    site.data = {**site.data, "webhook": webhook}

    record_event(
        db,
        site.tenant_id,
        site.user_id,
        "site",
        site.id,
        "webhook_alert",
        {
            "title": redacted_title,
            "format": parsed["format"],
            "status": parsed["status"],
            "case_id": case_id,
        },
    )
    db.commit()
    return {
        "accepted": True,
        "created_case": created,
        "case_id": case_id,
        "evidence_id": evidence_item["id"],
        "format": parsed["format"],
        "severity": parsed["severity"],
    }

"""历史 JSONL 预算账本对照迁移至 PostgreSQL / SQLite 数据库账本（支持幂等重跑与 100% 余额核对）。"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db.base import _make_engine
from app.db.budget_models import BudgetLedgerEvent, BudgetLedgerState, BudgetReservation
from app.services.budget_pg import DatabaseBudgetLedger, _clone_model_usage, _normalize_model_limits


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_timestamp(value) -> datetime:
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            pass
    return _now()


def parse_jsonl_ledger(path: str | Path, *, limit: int, model_limits: dict[str, int]) -> dict:
    """严格按 BudgetLedger._replay 规则解析历史 JSONL 并生成权威源快照。"""
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"源 JSONL 账本不存在：{source}")
    if isinstance(limit, bool) or int(limit) <= 0:
        raise ValueError("迁移目标总上限 limit 必须为正整数")
    normalized_limits = _normalize_model_limits(model_limits)
    token_limit = int(limit)

    raw = source.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ValueError("源 JSONL 末行未正常终止（可能写入中断），拒绝迁移")
    digest = hashlib.sha256(raw).hexdigest()

    actual = 0
    unknown = 0
    in_flight = 0
    per_model = {m: {"actual": 0, "unknown": 0, "in_flight": 0} for m in normalized_limits}
    halted = False
    halted_reason = ""

    reservations: dict[str, dict] = {}
    reservation_order: list[str] = []
    events: list[dict] = []
    line_count = 0

    for lineno, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        line_count += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"源 JSONL 第 {lineno} 行 JSON 无效：{exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"源 JSONL 第 {lineno} 行不是 JSON 对象")

        kind = record.get("type")
        if kind == "reserved":
            rid = str(record.get("id") or "")
            model = str(record.get("model") or "")
            if not rid or not model or "estimate" not in record:
                raise ValueError(f"源 JSONL 第 {lineno} 行 reserved 缺少必填字段")
            if rid in reservations:
                raise ValueError(f"源 JSONL 第 {lineno} 行存在重复预留 ID：{rid}")
            est = int(record["estimate"])
            if est < 0:
                raise ValueError(f"源 JSONL 第 {lineno} 行 estimate 不能为负：{est}")
            bucket = per_model.setdefault(model, {"actual": 0, "unknown": 0, "in_flight": 0})
            in_flight += est
            bucket["in_flight"] += est
            reservations[rid] = {
                "id": rid,
                "kind": str(record.get("kind") or "unknown"),
                "model": model,
                "estimate": est,
                "actual": None,
                "status": "reserved",
                "over_reserve": False,
                "pid": int(record.get("pid") or 0),
                "note": "",
                "reserved_at": _parse_timestamp(record.get("at")),
                "settled_at": None,
                "settlement_key": None,
            }
            reservation_order.append(rid)
        elif kind == "committed":
            rid = str(record.get("id") or "")
            item = reservations.get(rid)
            if item is None:
                raise ValueError(f"源 JSONL 第 {lineno} 行 committed 找不到对应预留：{rid}")
            if item["status"] != "reserved":
                raise ValueError(f"源 JSONL 第 {lineno} 行预留 {rid} 重复结算（原状态 {item['status']}）")
            est = int(record["estimate"])
            act = int(record["actual"])
            model = str(record.get("model") or item["model"])
            if est != item["estimate"] or model != item["model"]:
                raise ValueError(f"源 JSONL 第 {lineno} 行 committed 与预留 {rid} 的 model/estimate 不一致")
            bucket = per_model.setdefault(model, {"actual": 0, "unknown": 0, "in_flight": 0})
            in_flight -= est
            bucket["in_flight"] -= est
            actual += act
            bucket["actual"] += act
            over = act > est
            settled_at = _parse_timestamp(record.get("at"))
            item.update(
                status="committed",
                actual=act,
                over_reserve=over,
                settled_at=settled_at,
                settlement_key=f"commit:{rid}",
            )
            if over:
                halted = True
                halted_reason = f"实报 {act} 超预留 {est}（记录 {rid}）"
                events.append(
                    {
                        "event_type": "halt",
                        "idempotency_key": f"jsonl:halt:{rid}",
                        "reservation_id": rid,
                        "pid": int(record.get("pid") or 0),
                        "note": halted_reason,
                        "detail": {"model": model, "estimate": est, "actual": act, "source_line": lineno},
                        "created_at": settled_at,
                    }
                )
        elif kind == "committed_unknown":
            rid = str(record.get("id") or "")
            item = reservations.get(rid)
            if item is None:
                raise ValueError(f"源 JSONL 第 {lineno} 行 committed_unknown 找不到对应预留：{rid}")
            if item["status"] != "reserved":
                raise ValueError(f"源 JSONL 第 {lineno} 行预留 {rid} 重复结算（原状态 {item['status']}）")
            est = int(record["estimate"])
            model = str(record.get("model") or item["model"])
            if est != item["estimate"] or model != item["model"]:
                raise ValueError(f"源 JSONL 第 {lineno} 行 committed_unknown 与预留 {rid} 不一致")
            bucket = per_model.setdefault(model, {"actual": 0, "unknown": 0, "in_flight": 0})
            in_flight -= est
            bucket["in_flight"] -= est
            unknown += est
            bucket["unknown"] += est
            item.update(
                status="committed_unknown",
                settled_at=_parse_timestamp(record.get("at")),
                settlement_key=f"unknown:{rid}",
            )
        elif kind == "released":
            rid = str(record.get("id") or "")
            item = reservations.get(rid)
            if item is None:
                raise ValueError(f"源 JSONL 第 {lineno} 行 released 找不到对应预留：{rid}")
            if item["status"] != "reserved":
                raise ValueError(f"源 JSONL 第 {lineno} 行预留 {rid} 重复状态变更（原状态 {item['status']}）")
            est = int(record["estimate"])
            model = str(record.get("model") or item["model"])
            if est != item["estimate"] or model != item["model"]:
                raise ValueError(f"源 JSONL 第 {lineno} 行 released 与预留 {rid} 不一致")
            bucket = per_model.setdefault(model, {"actual": 0, "unknown": 0, "in_flight": 0})
            in_flight -= est
            bucket["in_flight"] -= est
            item.update(
                status="released",
                note=str(record.get("reason") or ""),
                settled_at=_parse_timestamp(record.get("at")),
                settlement_key=f"release:{rid}",
            )
        elif kind == "clearance":
            halted = False
            halted_reason = ""
            note = str(record.get("note") or "")
            line_hash = hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]
            events.append(
                {
                    "event_type": "clearance",
                    "idempotency_key": f"jsonl:clearance:{lineno}:{line_hash}",
                    "reservation_id": None,
                    "pid": int(record.get("pid") or 0),
                    "note": note,
                    "detail": {"source_line": lineno},
                    "created_at": _parse_timestamp(record.get("at")),
                }
            )
        else:
            raise ValueError(f"源 JSONL 第 {lineno} 行未知账本记录类型：{kind!r}")

    used = actual + unknown + in_flight
    model_remaining = {
        m: cap - (per_model[m]["actual"] + per_model[m]["unknown"] + per_model[m]["in_flight"])
        for m, cap in normalized_limits.items()
    }
    unsettled_ids = sorted(rid for rid, item in reservations.items() if item["status"] == "reserved")

    summary = {
        "limit": token_limit,
        "model_limits": dict(normalized_limits),
        "actual": actual,
        "unknown": unknown,
        "in_flight": in_flight,
        "remaining": token_limit - used,
        "models": {m: dict(v) for m, v in per_model.items()},
        "model_remaining": model_remaining,
        "halted": halted,
        "halted_reason": halted_reason,
        "unsettled_ids": unsettled_ids,
        "reservation_count": len(reservation_order),
        "event_count": len(events),
    }
    return {
        "source_path": str(source),
        "source_sha256": digest,
        "line_count": line_count,
        "reservations": [reservations[rid] for rid in reservation_order],
        "events": events,
        "summary": summary,
    }


def _upgrade_schema(eng) -> None:
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def _lock_or_create_scope(db, scope: str, limit: int, model_limits: dict[str, int]) -> BudgetLedgerState:
    dialect = getattr(getattr(db.get_bind(), "dialect", None), "name", "")
    row = None
    if dialect == "postgresql":
        row = db.query(BudgetLedgerState).filter_by(scope=scope).populate_existing().with_for_update().first()
    elif dialect == "sqlite":
        updated = db.execute(
            update(BudgetLedgerState).where(BudgetLedgerState.scope == scope).values(updated_at=_now())
        ).rowcount
        if updated:
            row = db.query(BudgetLedgerState).filter_by(scope=scope).populate_existing().first()
    else:
        row = db.query(BudgetLedgerState).filter_by(scope=scope).populate_existing().first()

    if row is None:
        now = _now()
        row = BudgetLedgerState(
            scope=scope,
            token_limit=limit,
            model_limits=dict(model_limits),
            actual_tokens=0,
            unknown_tokens=0,
            in_flight_tokens=0,
            model_usage=_clone_model_usage({}, model_limits),
            halted=False,
            halted_reason="",
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
    return row


def verify_migrated_ledger(source_summary: dict, ledger: DatabaseBudgetLedger) -> dict:
    """独立核对 DB 预算账本与源 JSONL 快照 100% 一致（总量/分模型/halt/余额/未结算 ID）。"""
    target_snap = ledger.verify_consistency()
    target_unsettled = sorted(ledger.list_unsettled_ids())

    checks = [
        ("actual", source_summary["actual"], target_snap["actual"]),
        ("unknown", source_summary["unknown"], target_snap["unknown"]),
        ("in_flight", source_summary["in_flight"], target_snap["in_flight"]),
        ("limit", source_summary["limit"], target_snap["limit"]),
        ("remaining", source_summary["remaining"], target_snap["remaining"]),
        ("model_limits", source_summary["model_limits"], target_snap["model_limits"]),
        ("model_remaining", source_summary["model_remaining"], target_snap["model_remaining"]),
        ("halted", source_summary["halted"], target_snap["halted"]),
        ("unsettled_ids", source_summary["unsettled_ids"], target_unsettled),
    ]
    mismatches = []
    for name, expected, got in checks:
        if expected != got:
            mismatches.append(f"{name}: expected={expected!r}, got={got!r}")

    zero_bucket = {"actual": 0, "unknown": 0, "in_flight": 0}
    all_models = sorted(set(source_summary["models"]) | set(target_snap["models"]))
    for model in all_models:
        exp_m = source_summary["models"].get(model, zero_bucket)
        got_m = target_snap["models"].get(model, zero_bucket)
        if exp_m != got_m:
            mismatches.append(f"model[{model}]: expected={exp_m!r}, got={got_m!r}")

    if mismatches:
        raise ValueError("历史 JSONL 迁移对照校验失败：" + "; ".join(mismatches))

    return {**target_snap, "unsettled_ids": target_unsettled}


def migrate_jsonl_to_db(
    jsonl_path: str | Path,
    *,
    limit: int,
    model_limits: dict[str, int],
    scope: str = "default",
    database_url: str | None = None,
    session_factory=None,
    engine=None,
    dry_run: bool = False,
    auto_upgrade: bool = False,
) -> dict:
    """将历史 JSONL 预算账本幂等导入数据库，并执行 100% 严格对照校验。"""
    parsed = parse_jsonl_ledger(jsonl_path, limit=limit, model_limits=model_limits)
    summary = parsed["summary"]

    if dry_run:
        return {
            "dry_run": True,
            "scope": scope,
            "source_path": parsed["source_path"],
            "source_sha256": parsed["source_sha256"],
            "line_count": parsed["line_count"],
            "source": summary,
            "verified": False,
        }

    owns_engine = False
    target_engine = engine
    if session_factory is None:
        if target_engine is None:
            url = database_url or Settings().database_url
            target_engine = _make_engine(url)
            owns_engine = True
        if auto_upgrade:
            _upgrade_schema(target_engine)
        session_factory = sessionmaker(bind=target_engine, autoflush=False, expire_on_commit=False)
    elif auto_upgrade and target_engine is not None:
        _upgrade_schema(target_engine)

    try:
        inserted_reservations = 0
        skipped_reservations = 0
        inserted_events = 0
        skipped_events = 0

        db = session_factory()
        try:
            state = _lock_or_create_scope(db, scope, summary["limit"], summary["model_limits"])

            existing_res = {
                row.id: row for row in db.query(BudgetReservation).filter_by(scope=scope).all()
            }
            source_res_ids = {item["id"] for item in parsed["reservations"]}
            unexpected_ids = set(existing_res) - source_res_ids
            if unexpected_ids:
                raise ValueError(
                    f"目标作用域 {scope!r} 已存在非本次源 JSONL 的预留记录（共 {len(unexpected_ids)} 条），拒绝覆盖"
                )

            for item in parsed["reservations"]:
                rid = item["id"]
                old = existing_res.get(rid)
                if old is not None:
                    same = (
                        old.kind == item["kind"]
                        and old.model == item["model"]
                        and int(old.estimate) == int(item["estimate"])
                        and old.status == item["status"]
                        and (None if old.actual is None else int(old.actual)) == item["actual"]
                        and bool(old.over_reserve) == bool(item["over_reserve"])
                    )
                    if not same:
                        raise ValueError(f"目标作用域 {scope!r} 中预留 {rid} 与源 JSONL 内容冲突，拒绝覆盖")
                    skipped_reservations += 1
                    continue

                db.add(
                    BudgetReservation(
                        id=rid,
                        scope=scope,
                        idempotency_key=rid,
                        settlement_key=item["settlement_key"],
                        kind=item["kind"],
                        model=item["model"],
                        estimate=item["estimate"],
                        actual=item["actual"],
                        status=item["status"],
                        over_reserve=item["over_reserve"],
                        pid=item["pid"],
                        note=item["note"],
                        reserved_at=item["reserved_at"],
                        settled_at=item["settled_at"],
                    )
                )
                inserted_reservations += 1

            existing_event_keys = {
                row[0]
                for row in db.query(BudgetLedgerEvent.idempotency_key).filter_by(scope=scope).all()
            }
            for ev in parsed["events"]:
                if ev["idempotency_key"] in existing_event_keys:
                    skipped_events += 1
                    continue
                db.add(
                    BudgetLedgerEvent(
                        scope=scope,
                        event_type=ev["event_type"],
                        idempotency_key=ev["idempotency_key"],
                        reservation_id=ev["reservation_id"],
                        pid=ev["pid"],
                        note=ev["note"],
                        detail=ev["detail"],
                        created_at=ev["created_at"],
                    )
                )
                inserted_events += 1

            import_event_key = f"jsonl:import:{parsed['source_sha256']}"
            if import_event_key not in existing_event_keys:
                db.add(
                    BudgetLedgerEvent(
                        scope=scope,
                        event_type="migration",
                        idempotency_key=import_event_key,
                        reservation_id=None,
                        pid=os.getpid(),
                        note=f"从历史 JSONL 导入：{parsed['source_path']}",
                        detail={
                            "source_sha256": parsed["source_sha256"],
                            "reservation_count": summary["reservation_count"],
                            "unsettled_count": len(summary["unsettled_ids"]),
                        },
                        created_at=_now(),
                    )
                )

            state.token_limit = summary["limit"]
            state.model_limits = dict(summary["model_limits"])
            state.actual_tokens = summary["actual"]
            state.unknown_tokens = summary["unknown"]
            state.in_flight_tokens = summary["in_flight"]
            state.model_usage = _clone_model_usage(summary["models"], summary["model_limits"])
            state.halted = summary["halted"]
            state.halted_reason = summary["halted_reason"]
            state.updated_at = _now()
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        # 独立只读恢复实例执行 100% 对照校验（验证持久化后的额度上限、余额、halt 与未结算记录）。
        verify_ledger = DatabaseBudgetLedger(scope=scope, session_factory=session_factory)
        target = verify_migrated_ledger(summary, verify_ledger)

        return {
            "dry_run": False,
            "scope": scope,
            "source_path": parsed["source_path"],
            "source_sha256": parsed["source_sha256"],
            "line_count": parsed["line_count"],
            "inserted_reservations": inserted_reservations,
            "skipped_reservations": skipped_reservations,
            "inserted_events": inserted_events,
            "skipped_events": skipped_events,
            "source": summary,
            "target": target,
            "unsettled_ids": target["unsettled_ids"],
            "verified": True,
        }
    finally:
        if owns_engine and target_engine is not None:
            target_engine.dispose()


def _parse_cli_model_limits(raw: str | None, fallback: dict[str, int]) -> dict[str, int]:
    if not raw:
        return dict(fallback)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("--model-limits 必须为 JSON 对象（例如 '{\"qwen3.8-max\": 300000}'）")
    return {str(k): int(v) for k, v in parsed.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jsonl", required=True, type=Path, help="源历史 JSONL 预算账本路径")
    parser.add_argument("--database-url", default=None, help="目标数据库连接串（默认读取 Settings.database_url）")
    parser.add_argument("--scope", default="default", help="数据库预算账本作用域（默认 default）")
    parser.add_argument("--limit", type=int, default=None, help="总授权 token 上限（默认读取 Settings.budget_token_limit）")
    parser.add_argument(
        "--model-limits",
        default=None,
        help="分模型授权上限 JSON 字典（默认读取 Settings.budget_model_limits）",
    )
    parser.add_argument("--auto-upgrade", action="store_true", help="迁移前先自动执行 Alembic upgrade head")
    parser.add_argument("--dry-run", action="store_true", help="只解析并核对源 JSONL，不写入数据库")
    args = parser.parse_args()

    settings = Settings()
    limit = args.limit if args.limit is not None else int(settings.budget_token_limit)
    model_limits = _parse_cli_model_limits(args.model_limits, settings.budget_model_limits)

    report = migrate_jsonl_to_db(
        args.jsonl,
        limit=limit,
        model_limits=model_limits,
        scope=args.scope,
        database_url=args.database_url or settings.database_url,
        dry_run=args.dry_run,
        auto_upgrade=args.auto_upgrade,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

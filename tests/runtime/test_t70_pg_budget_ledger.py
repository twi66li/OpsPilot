"""T70 验收测试：数据库事务预算账本（PG/SQLite）、并发预占、幂等键、halt/clearance、重启恢复与历史 JSONL 对照迁移。"""

import json
import multiprocessing
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.config import Settings
from app.db.budget_models import BudgetLedgerEvent, BudgetReservation
from app.services import budget as budget_mod
from app.services.budget import (
    BudgetExceededError,
    BudgetLedger,
    BudgetLedgerError,
    DatabaseBudgetLedger,
    get_budget_guard,
    reset_budget_guard,
    resolve_budget_backend,
)
from app.services.model_api import ChatAPI
from app.services.run_service import RunCancelled
from scripts.migrate_budget_ledger import migrate_jsonl_to_db, parse_jsonl_ledger

UNREACHABLE = "http://127.0.0.1:9/v1"


def _init_sqlite_db(db_path: Path) -> str:
    url = "sqlite:///" + db_path.as_posix()
    eng = create_engine(url)
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        with eng.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
    finally:
        eng.dispose()
    return url


def _mp_reserve_worker(
    database_url: str,
    scope: str,
    limit: int,
    model_limits: dict[str, int],
    model: str,
    estimate: int,
    attempts: int,
    barrier,
    result_queue,
):
    """独立子进程竞争预占 worker（spawn 可序列化）。"""
    ledger = DatabaseBudgetLedger(
        scope=scope,
        limit=limit,
        model_limits=model_limits,
        database_url=database_url,
    )
    succeeded = []
    refused = 0
    errors = []
    try:
        barrier.wait(timeout=30)
        for _ in range(attempts):
            try:
                rid = ledger.reserve("chat", model, estimate)
                succeeded.append(rid)
            except BudgetExceededError:
                refused += 1
            except Exception as exc:  # pragma: no cover - 断言无意外异常
                errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        ledger.close()
    result_queue.put({"pid": os.getpid(), "succeeded": succeeded, "refused": refused, "errors": errors})


@pytest.fixture(autouse=True)
def _cleanup_guard_cache():
    yield
    for guard in list(budget_mod._guards.values()):
        guard.close()
    reset_budget_guard()


# ---- 1. Alembic 0006 升级/降级与约束验证 ----


def test_migration_0006_upgrade_and_downgrade(tmp_path):
    url = "sqlite:///" + (tmp_path / "mig_0006.db").as_posix()
    eng = create_engine(url)
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        with eng.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "0005")
            assert "budget_ledgers" not in inspect(conn).get_table_names()

            command.upgrade(cfg, "0006")
            tables = set(inspect(conn).get_table_names())
            assert {"budget_ledgers", "budget_reservations", "budget_ledger_events"} <= tables
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"

            command.downgrade(cfg, "0005")
            tables_after = set(inspect(conn).get_table_names())
            assert "budget_ledgers" not in tables_after
            assert "budget_reservations" not in tables_after
            assert "budget_ledger_events" not in tables_after

            command.upgrade(cfg, "head")
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
    finally:
        eng.dispose()


# ---- 2. 双独立进程竞争预占不超额（无 OS 锁冲突） ----


def test_two_independent_processes_compete_without_overshoot(tmp_path):
    db_url = _init_sqlite_db(tmp_path / "mp_budget.db")
    scope = "mp-total-cap"
    limit = 1000
    model_limits = {"qwen3.8-max": 1000}

    # 预先初始化作用域，验证双进程共享同一数据库账本无 OS 单写者锁冲突
    init_ledger = DatabaseBudgetLedger(scope=scope, limit=limit, model_limits=model_limits, database_url=db_url)
    init_ledger.close()

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    procs = [
        ctx.Process(
            target=_mp_reserve_worker,
            args=(db_url, scope, limit, model_limits, "qwen3.8-max", 100, 15, barrier, queue),
        )
        for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    results = [queue.get(timeout=60) for _ in range(2)]
    for proc in procs:
        proc.join(timeout=30)
        assert proc.exitcode == 0

    assert results[0]["pid"] != results[1]["pid"]
    assert results[0]["errors"] == [] and results[1]["errors"] == []

    all_rids = results[0]["succeeded"] + results[1]["succeeded"]
    total_refused = results[0]["refused"] + results[1]["refused"]
    assert len(all_rids) == 10
    assert len(set(all_rids)) == 10
    assert total_refused == 20

    checker = DatabaseBudgetLedger(scope=scope, database_url=db_url)
    try:
        snap = checker.verify_consistency()
        assert snap["in_flight"] == 1000
        assert snap["actual"] == 0
        assert snap["unknown"] == 0
        assert snap["remaining"] == 0
        assert snap["models"]["qwen3.8-max"]["in_flight"] == 1000
    finally:
        checker.close()


def test_two_independent_processes_respect_per_model_cap(tmp_path):
    db_url = _init_sqlite_db(tmp_path / "mp_model_cap.db")
    scope = "mp-model-cap"
    limit = 2000
    model_limits = {"model-a": 500, "model-b": 2000}

    DatabaseBudgetLedger(scope=scope, limit=limit, model_limits=model_limits, database_url=db_url).close()

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    procs = [
        ctx.Process(
            target=_mp_reserve_worker,
            args=(db_url, scope, limit, model_limits, "model-a", 100, 10, barrier, queue),
        )
        for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    results = [queue.get(timeout=60) for _ in range(2)]
    for proc in procs:
        proc.join(timeout=30)
        assert proc.exitcode == 0

    assert results[0]["errors"] == [] and results[1]["errors"] == []
    assert len(results[0]["succeeded"]) + len(results[1]["succeeded"]) == 5
    assert results[0]["refused"] + results[1]["refused"] == 15

    checker = DatabaseBudgetLedger(scope=scope, database_url=db_url)
    try:
        # model-a 已满，model-b 仍可在其自身额度内独立预占
        with pytest.raises(BudgetExceededError, match="模型 model-a 额度超限"):
            checker.reserve("chat", "model-a", 1)
        rid_b = checker.reserve("chat", "model-b", 300)
        assert rid_b
        snap = checker.verify_consistency()
        assert snap["in_flight"] == 800
        assert snap["models"]["model-a"]["in_flight"] == 500
        assert snap["models"]["model-b"]["in_flight"] == 300
    finally:
        checker.close()


# ---- 3. 重复预占/重复结算/重复释放幂等、已结算不可释放、超时 unknown 与未发送 release ----


def test_idempotent_reserve_commit_unknown_and_release_rules(tmp_path):
    db_url = _init_sqlite_db(tmp_path / "idem.db")
    ledger = DatabaseBudgetLedger(
        scope="idem-scope",
        limit=1000,
        model_limits={"m1": 1000},
        database_url=db_url,
    )
    try:
        # 1) 重复预占幂等：返回相同 rid 且只扣一次 in_flight
        rid1 = ledger.reserve("chat", "m1", 200, idempotency_key="req-001")
        rid1_dup = ledger.reserve("chat", "m1", 200, idempotency_key="req-001")
        assert rid1_dup == rid1
        assert ledger.usage_now()["in_flight"] == 200

        # 同幂等键不同参数必须拒绝
        with pytest.raises(BudgetLedgerError, match="预占幂等键"):
            ledger.reserve("chat", "m1", 250, idempotency_key="req-001")

        # 2) 重复结算幂等；不同参数覆盖拒绝；已结算禁止释放
        res_first = ledger.commit(rid1, {"total_tokens": 120}, idempotency_key="settle-001")
        res_second = ledger.commit(rid1, {"total_tokens": 120}, idempotency_key="settle-001")
        res_third = ledger.commit(rid1, 120)
        assert res_first["idempotent"] is False
        assert res_second["idempotent"] is True
        assert res_third["idempotent"] is True
        assert ledger.usage_now()["actual"] == 120
        assert ledger.usage_now()["in_flight"] == 0

        with pytest.raises(BudgetLedgerError, match="拒绝重复覆盖"):
            ledger.commit(rid1, {"total_tokens": 130}, idempotency_key="settle-001")
        with pytest.raises(BudgetLedgerError, match="不可释放"):
            ledger.release(rid1, reason="late cancel")
        with pytest.raises(BudgetLedgerError, match="终态"):
            ledger.commit_unknown(rid1)

        # 3) 超时/缺 usage -> commit_unknown 保留风险额度，重复幂等，且不可释放
        rid2 = ledger.reserve("chat", "m1", 300, idempotency_key="req-002")
        unk1 = ledger.commit_unknown(rid2, idempotency_key="unk-002", reason="provider timeout")
        unk2 = ledger.commit_unknown(rid2, idempotency_key="unk-002", reason="provider timeout")
        assert unk1["idempotent"] is False and unk2["idempotent"] is True
        snap = ledger.usage_now()
        assert snap["unknown"] == 300 and snap["in_flight"] == 0 and snap["remaining"] == 580

        with pytest.raises(BudgetLedgerError, match="不可释放"):
            ledger.release(rid2)
        with pytest.raises(BudgetLedgerError, match="终态"):
            ledger.commit(rid2, 50)

        # 4) 未发送 -> release 释放额度，重复释放幂等，已释放不可再结算
        rid3 = ledger.reserve("chat", "m1", 250, idempotency_key="req-003")
        assert ledger.usage_now()["in_flight"] == 250
        rel1 = ledger.release(rid3, idempotency_key="rel-003", reason="cancelled before send")
        rel2 = ledger.release(rid3, idempotency_key="rel-003", reason="cancelled before send")
        rel3 = ledger.release(rid3)
        assert rel1["idempotent"] is False and rel2["idempotent"] is True and rel3["idempotent"] is True
        snap = ledger.usage_now()
        assert snap["in_flight"] == 0 and snap["actual"] == 120 and snap["unknown"] == 300
        assert snap["remaining"] == 580

        with pytest.raises(BudgetLedgerError, match="终态"):
            ledger.commit(rid3, 10)
        with pytest.raises(BudgetLedgerError, match="终态"):
            ledger.commit_unknown(rid3)

        # 5) 不存在的 rid 拒绝结算/释放；commit 拒绝缺 usage（防止误记 0）
        with pytest.raises(BudgetLedgerError):
            ledger.commit("non-existent", 10)
        with pytest.raises(BudgetLedgerError):
            ledger.release("non-existent")
        rid4 = ledger.reserve("chat", "m1", 50)
        with pytest.raises(BudgetLedgerError, match="commit_unknown"):
            ledger.commit(rid4, None)
        ledger.release(rid4)

        ledger.verify_consistency()
    finally:
        ledger.close()


# ---- 4. 实报超预留触发 halt、跨实例持久化生效、在途继续结算与 clearance 恢复 ----


def test_over_reserve_halt_persists_and_recovers_only_via_clearance(tmp_path):
    db_url = _init_sqlite_db(tmp_path / "halt.db")
    replica_a = DatabaseBudgetLedger(scope="halt-scope", limit=1000, model_limits={"m1": 1000}, database_url=db_url)
    replica_b = DatabaseBudgetLedger(scope="halt-scope", limit=1000, model_limits={"m1": 1000}, database_url=db_url)
    try:
        rid1 = replica_a.reserve("chat", "m1", 100)
        rid2 = replica_b.reserve("chat", "m1", 200)

        # 副本 A 实报超预留 -> 立即 halt
        out = replica_a.commit(rid1, {"total_tokens": 160})
        assert out["over_reserve"] is True
        assert replica_a.halted is True
        assert replica_b.halted is True

        # 副本 B 发起新预占立即被拒
        with pytest.raises(BudgetExceededError, match="预算已停止"):
            replica_b.reserve("chat", "m1", 1)

        # 已在途的 rid2 仍可精确结算
        replica_b.commit(rid2, {"total_tokens": 180})
        assert replica_b.usage_now()["actual"] == 340
        assert replica_b.usage_now()["in_flight"] == 0

        # 重建实例不等于人工核查，halt 仍然持久生效
        replica_a.close()
        replica_b.close()
        restarted = DatabaseBudgetLedger(scope="halt-scope", database_url=db_url)
        try:
            assert restarted.halted is True
            with pytest.raises(BudgetExceededError, match="预算已停止"):
                restarted.reserve("chat", "m1", 1)

            restarted.clear_halt("人工核查确认供应商统计口径差异，批准恢复", idempotency_key="clear-001")
            restarted.clear_halt("重复解除幂等", idempotency_key="clear-001")
            assert restarted.halted is False

            # 恢复后可正常预占并记账，审计表有 halt + clearance
            rid3 = restarted.reserve("chat", "m1", 50)
            restarted.commit(rid3, {"total_tokens": 40})
            snap = restarted.verify_consistency()
            assert snap["actual"] == 380 and snap["halted"] is False

            with restarted._session_factory() as db:
                events = (
                    db.query(BudgetLedgerEvent)
                    .filter_by(scope="halt-scope")
                    .order_by(BudgetLedgerEvent.id.asc())
                    .all()
                )
                assert [e.event_type for e in events] == ["halt", "clearance"]
        finally:
            restarted.close()
    finally:
        replica_a.close()
        replica_b.close()


# ---- 5. 重启恢复后额度上限、余额、未结算记录 100% 一致 ----


def test_restart_recovery_preserves_limits_balances_and_unsettled(tmp_path):
    db_url = _init_sqlite_db(tmp_path / "restart.db")
    scope = "restart-scope"
    limits = {"qwen3.8-max": 6000, "qwen3.7-text-embedding-flash": 5000}

    first = DatabaseBudgetLedger(scope=scope, limit=10_000, model_limits=limits, database_url=db_url)
    rid_commit = first.reserve("chat", "qwen3.8-max", 1000)
    first.commit(rid_commit, {"total_tokens": 750})
    rid_unknown = first.reserve("chat", "qwen3.8-max", 500)
    first.commit_unknown(rid_unknown, reason="timeout")
    rid_release = first.reserve("embedding", "qwen3.7-text-embedding-flash", 400)
    first.release(rid_release, reason="cancelled")
    rid_inflight = first.reserve("embedding", "qwen3.7-text-embedding-flash", 600)
    before = first.verify_consistency()
    first.close()

    # 方式 A：纯从 DB 恢复（不传 limit / model_limits）
    recovered_from_db = DatabaseBudgetLedger(scope=scope, database_url=db_url)
    try:
        after_a = recovered_from_db.verify_consistency()
        assert after_a == before
        assert after_a["limit"] == 10_000
        assert after_a["model_limits"] == limits
        assert after_a["actual"] == 750
        assert after_a["unknown"] == 500
        assert after_a["in_flight"] == 600
        assert after_a["remaining"] == 8150
        assert after_a["model_remaining"] == {"qwen3.8-max": 4750, "qwen3.7-text-embedding-flash": 4400}
        assert recovered_from_db.list_unsettled_ids() == [rid_inflight]
    finally:
        recovered_from_db.close()

    # 方式 B：携带配置重启，历史余额与未结算记录继续完整保留
    recovered_with_cfg = DatabaseBudgetLedger(scope=scope, limit=10_000, model_limits=limits, database_url=db_url)
    try:
        assert recovered_with_cfg.verify_consistency() == before
        recovered_with_cfg.commit(rid_inflight, {"total_tokens": 550})
        final_snap = recovered_with_cfg.verify_consistency()
        assert final_snap["actual"] == 1300
        assert final_snap["unknown"] == 500
        assert final_snap["in_flight"] == 0
        assert final_snap["remaining"] == 8200
    finally:
        recovered_with_cfg.close()


# ---- 6. 账本工厂与 ChatAPI 接线（PG 自动路由、显式 db、发送前取消 release、发送后异常 unknown） ----


def test_budget_backend_routing_and_chat_api_integration(tmp_path, monkeypatch):
    # PG URL 在 auto 模式下自动走 db
    pg_settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://opspilot:pass@127.0.0.1:25433/opspilot",
        budget_ledger_backend="auto",
        budget_token_limit=50_000,
        budget_model_limits={"m1": 50_000},
    )
    assert resolve_budget_backend(pg_settings) == "db"

    # PG URL 未配预算额度 -> none（真实入口拒绝启动）
    pg_no_budget = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://opspilot:pass@127.0.0.1:25433/opspilot",
        budget_ledger_backend="auto",
    )
    assert resolve_budget_backend(pg_no_budget) == "none"

    # SQLite 显式 db 后端无缝装配到 ChatAPI
    db_url = _init_sqlite_db(tmp_path / "wiring.db")
    db_settings = Settings(
        _env_file=None,
        database_url=db_url,
        budget_ledger_backend="db",
        budget_ledger_scope="api-wiring",
        budget_token_limit=100_000,
        budget_model_limits={"model-primary": 100, "model-backup": 100_000},
        chat_model_candidates=["model-backup"],
    )
    reset_budget_guard()
    monkeypatch.delenv("CHAT_MODEL_CANDIDATES", raising=False)
    monkeypatch.setattr("app.config.get_settings", lambda: db_settings)

    guard = get_budget_guard()
    assert isinstance(guard, DatabaseBudgetLedger)
    assert get_budget_guard() is guard

    api = ChatAPI("key", UNREACHABLE, "model-primary")
    assert api._budget is guard

    # 1) 发送前取消 -> release 释放额度（in_flight/unknown/actual 全归 0）
    def _cancel_on_emit(kind, detail):
        if kind == "model_input":
            raise RunCancelled("发送前取消")

    monkeypatch.setattr("app.services.model_api.emit", _cancel_on_emit)
    with pytest.raises(RunCancelled):
        api._chat_once("model-backup", "sys", "usr", output_cap=64)
    snap_after_cancel = guard.verify_consistency()
    assert snap_after_cancel["actual"] == 0
    assert snap_after_cancel["unknown"] == 0
    assert snap_after_cancel["in_flight"] == 0
    assert api.calls[-1]["budget_action"] == "released"

    # 2) 恢复正常 emit，model-primary 分模型超限自动切到 model-backup，打不可达地址 -> commit_unknown 保留风险额度
    monkeypatch.setattr("app.services.model_api.emit", lambda *a, **k: None)
    with pytest.raises(Exception):
        api.chat("sys", "usr", output_cap=64)
    assert any(c.get("status") == "model_switch" and c.get("to") == "model-backup" for c in api.calls)
    snap_after_err = guard.verify_consistency()
    assert snap_after_err["unknown"] > 0
    assert snap_after_err["in_flight"] == 0
    assert snap_after_err["models"]["model-primary"]["unknown"] == 0
    assert snap_after_err["models"]["model-backup"]["unknown"] == snap_after_err["unknown"]

    # 3) 模拟供应商正常流式返回 usage -> commit 入账 actual
    class _FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            yield SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(content="诊断结果"), finish_reason="stop")],
            )
            yield SimpleNamespace(
                usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}),
                choices=[],
            )

    monkeypatch.setattr(api.client.chat.completions, "create", lambda **kwargs: _FakeStream())
    out = api.chat("sys", "usr", output_cap=64)
    assert out == "诊断结果"
    snap_after_ok = guard.verify_consistency()
    assert snap_after_ok["actual"] == 30
    assert snap_after_ok["models"]["model-backup"]["actual"] == 30


# ---- 7. 历史 JSONL 对照迁移校验（含未结算记录、halt/clearance、幂等重跑与真实历史账本） ----


def test_migrate_jsonl_matches_legacy_ledger_and_is_idempotent(tmp_path):
    jsonl_path = tmp_path / "legacy_ledger.jsonl"
    limit = 50_000
    model_limits = {"m-chat": 30_000, "m-embed": 20_000}

    # 用原有 JSONL BudgetLedger 构造涵盖全部状态的历史账本
    legacy = BudgetLedger(str(jsonl_path), limit, model_limits)
    r_ok = legacy.reserve("chat", "m-chat", 1000)
    legacy.settle(r_ok, {"total_tokens": 620})
    r_unk = legacy.reserve("chat", "m-chat", 800)
    legacy.settle(r_unk, None)
    r_rel = legacy.reserve("embedding", "m-embed", 500)
    legacy.release(r_rel, reason="cancelled")
    r_over = legacy.reserve("chat", "m-chat", 200)
    legacy.settle(r_over, {"total_tokens": 350})  # 触发 halt
    assert legacy.halted is True
    legacy.clear_halt("首次人工核查解除")
    r_over2 = legacy.reserve("embedding", "m-embed", 100)
    legacy.settle(r_over2, {"total_tokens": 150})  # 再次触发 halt，保留为 halted=True
    r_unsettled = legacy.reserve("embedding", "m-embed", 0) if False else None
    legacy.close()

    # 追加一条历史未结算 reserved 记录（在 halt 前已经预占的在途记录）
    raw_lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    unsettled_record = {
        "type": "reserved",
        "id": "unsettled_legacy_001",
        "at": "2026-11-01T10:00:00.000+00:00",
        "pid": 12345,
        "kind": "embedding",
        "model": "m-embed",
        "estimate": 700,
    }
    raw_lines.insert(2, json.dumps(unsettled_record, ensure_ascii=False))
    jsonl_path.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")

    # 用原生 BudgetLedger 重放作为唯一对照基线
    ref = BudgetLedger(str(jsonl_path), limit, model_limits)
    expected = ref.usage_now()
    ref.close()

    db_url = _init_sqlite_db(tmp_path / "migrated.db")
    report1 = migrate_jsonl_to_db(
        jsonl_path,
        limit=limit,
        model_limits=model_limits,
        scope="hist-001",
        database_url=db_url,
    )
    assert report1["verified"] is True
    assert report1["inserted_reservations"] == 6
    assert report1["skipped_reservations"] == 0
    assert report1["unsettled_ids"] == ["unsettled_legacy_001"]

    target = report1["target"]
    assert target["actual"] == expected["actual"] == 1120
    assert target["unknown"] == expected["unknown"] == 800
    assert target["in_flight"] == expected["in_flight"] == 700
    assert target["remaining"] == limit - (1120 + 800 + 700)
    assert target["models"] == expected["models"]
    assert target["halted"] is True

    # 幂等重跑：0 新增、全跳过、余额 100% 不变
    report2 = migrate_jsonl_to_db(
        jsonl_path,
        limit=limit,
        model_limits=model_limits,
        scope="hist-001",
        database_url=db_url,
    )
    assert report2["verified"] is True
    assert report2["inserted_reservations"] == 0
    assert report2["skipped_reservations"] == 6
    assert report2["inserted_events"] == 0
    assert report2["target"] == target

    # 迁移后的历史未结算记录可继续在 DB 账本中正常结算，且 halt 需显式 clearance 解除
    db_ledger = DatabaseBudgetLedger(scope="hist-001", database_url=db_url)
    try:
        assert db_ledger.halted is True
        with pytest.raises(BudgetExceededError):
            db_ledger.reserve("chat", "m-chat", 1)
        db_ledger.commit_unknown("unsettled_legacy_001", reason="迁移后收口历史遗留")
        db_ledger.clear_halt("核查迁移后的 halt 并放行")
        snap = db_ledger.verify_consistency()
        assert snap["in_flight"] == 0
        assert snap["unknown"] == 1500
        assert snap["halted"] is False
    finally:
        db_ledger.close()
    assert r_unsettled is None


def test_migrate_rejects_corrupt_jsonl_and_foreign_scope_collision(tmp_path):
    bad_jsonl = tmp_path / "corrupt.jsonl"
    bad_jsonl.write_bytes(b'{"type":"reserved","id":"r1","model":"m1","estimate":10}')  # 缺换行
    with pytest.raises(ValueError, match="末行未正常终止"):
        parse_jsonl_ledger(bad_jsonl, limit=1000, model_limits={"m1": 1000})

    db_url = _init_sqlite_db(tmp_path / "collision.db")
    ledger = DatabaseBudgetLedger(scope="busy", limit=1000, model_limits={"m1": 1000}, database_url=db_url)
    ledger.reserve("chat", "m1", 50)
    ledger.close()

    good_jsonl = tmp_path / "good.jsonl"
    good_jsonl.write_text(
        json.dumps({"type": "reserved", "id": "other-id", "model": "m1", "estimate": 10, "kind": "chat"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="非本次源 JSONL"):
        migrate_jsonl_to_db(good_jsonl, limit=1000, model_limits={"m1": 1000}, scope="busy", database_url=db_url)


def test_migrate_real_historical_eval_jsonl_matches_100_percent(tmp_path):
    """只读验证仓库真实历史账本 eval/night20261102q/ledger.jsonl 迁移前后 100% 一致。"""
    hist_path = Path(__file__).resolve().parents[2] / "eval" / "night20261102q" / "ledger.jsonl"
    if not hist_path.exists():
        pytest.skip("历史评测账本文件不存在")

    # 仅只读解析历史中涉及的所有模型，并复制到临时路径给原生 BudgetLedger 重放（绝不触碰 eval 目录锁文件）
    copied = tmp_path / "night20261102q_ledger.jsonl"
    copied.write_bytes(hist_path.read_bytes())
    models = sorted(
        {
            json.loads(line)["model"]
            for line in copied.read_text(encoding="utf-8").splitlines()
            if line.strip() and "model" in json.loads(line)
        }
    )
    limit = 10_000_000
    model_limits = {m: 5_000_000 for m in models}

    ref = BudgetLedger(str(copied), limit, model_limits)
    expected = ref.usage_now()
    ref.close()

    db_url = _init_sqlite_db(tmp_path / "hist_real.db")
    report = migrate_jsonl_to_db(copied, limit=limit, model_limits=model_limits, scope="night20261102q", database_url=db_url)
    assert report["verified"] is True
    assert report["target"]["actual"] == expected["actual"]
    assert report["target"]["unknown"] == expected["unknown"]
    assert report["target"]["in_flight"] == expected["in_flight"]
    assert report["target"]["models"] == expected["models"]
    assert report["target"]["halted"] == expected["halted"]

    # 同时验证明细表行数与 JSONL 一致
    eng = create_engine(db_url)
    try:
        with eng.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM budget_reservations WHERE scope = :s"), {"s": "night20261102q"}
            ).scalar()
            assert count == report["source"]["reservation_count"] > 100
    finally:
        eng.dispose()


def test_migrate_cli_dry_run_and_apply(tmp_path, monkeypatch, capsys):
    jsonl_path = tmp_path / "cli.jsonl"
    jsonl_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "reserved", "id": "c1", "kind": "chat", "model": "m1", "estimate": 100}),
                json.dumps({"type": "committed", "id": "c1", "model": "m1", "estimate": 100, "actual": 60}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    db_url = "sqlite:///" + (tmp_path / "cli_target.db").as_posix()

    from scripts import migrate_budget_ledger as cli_mod

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_budget_ledger.py",
            "--jsonl",
            str(jsonl_path),
            "--database-url",
            db_url,
            "--scope",
            "cli-s1",
            "--limit",
            "1000",
            "--model-limits",
            '{"m1": 1000}',
            "--dry-run",
        ],
    )
    cli_mod.main()
    dry_out = json.loads(capsys.readouterr().out)
    assert dry_out["dry_run"] is True and dry_out["source"]["actual"] == 60

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_budget_ledger.py",
            "--jsonl",
            str(jsonl_path),
            "--database-url",
            db_url,
            "--scope",
            "cli-s1",
            "--limit",
            "1000",
            "--model-limits",
            '{"m1": 1000}',
            "--auto-upgrade",
        ],
    )
    cli_mod.main()
    apply_out = json.loads(capsys.readouterr().out)
    assert apply_out["verified"] is True and apply_out["target"]["actual"] == 60
    assert BudgetReservation.__tablename__ == "budget_reservations"


def test_postgres_dialect_lock_query_compiles_for_update():
    """验证 PostgreSQL 方言下作用域行锁查询显式生成 SELECT ... FOR UPDATE。"""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import Session

    from app.db.budget_models import BudgetLedgerState

    dummy_engine = create_engine("sqlite:///:memory:")
    try:
        with Session(dummy_engine) as db:
            query = db.query(BudgetLedgerState).filter_by(scope="pg-check").with_for_update()
            compiled = str(query.statement.compile(dialect=postgresql.dialect()))
            assert "FOR UPDATE" in compiled.upper()
            assert "budget_ledgers" in compiled
    finally:
        dummy_engine.dispose()


def test_real_postgres16_end_to_end_when_available(tmp_path):
    """当设置了 OPSPILOT_T70_PG_URL 时，针对真实 PostgreSQL 16 运行全链路双进程竞争与迁移验收。"""
    pg_url = os.environ.get("OPSPILOT_T70_PG_URL", "").strip()
    if not pg_url:
        pytest.skip("未设置 OPSPILOT_T70_PG_URL（等待 Docker 启动后补跑真实 PostgreSQL 16 验收）")

    eng = create_engine(pg_url)
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        with eng.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
    finally:
        eng.dispose()

    scope = f"pg16-mp-{os.getpid()}"
    limit = 1000
    model_limits = {"qwen3.8-max": 1000}
    DatabaseBudgetLedger(scope=scope, limit=limit, model_limits=model_limits, database_url=pg_url).close()

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    procs = [
        ctx.Process(
            target=_mp_reserve_worker,
            args=(pg_url, scope, limit, model_limits, "qwen3.8-max", 100, 15, barrier, queue),
        )
        for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    results = [queue.get(timeout=60) for _ in range(2)]
    for proc in procs:
        proc.join(timeout=30)
        assert proc.exitcode == 0

    assert results[0]["errors"] == [] and results[1]["errors"] == []
    assert len(results[0]["succeeded"]) + len(results[1]["succeeded"]) == 10
    assert results[0]["refused"] + results[1]["refused"] == 20

    # 幂等、commit_unknown、release、halt/clearance、历史 JSONL 迁移对照
    ledger = DatabaseBudgetLedger(scope=scope, database_url=pg_url)
    try:
        snap = ledger.verify_consistency()
        assert snap["in_flight"] == 1000 and snap["remaining"] == 0
        r0 = results[0]["succeeded"][0]
        r1 = results[0]["succeeded"][1] if len(results[0]["succeeded"]) > 1 else results[1]["succeeded"][0]
        ledger.release(r0)
        ledger.release(r0)  # 重复释放幂等
        ledger.commit_unknown(r1, reason="pg timeout")
        with pytest.raises(BudgetLedgerError):
            ledger.release(r1)  # 已结算不可释放
        r2 = ledger.reserve("chat", "qwen3.8-max", 50, idempotency_key="pg-idem-1")
        assert ledger.reserve("chat", "qwen3.8-max", 50, idempotency_key="pg-idem-1") == r2
        ledger.commit(r2, {"total_tokens": 90})  # 超预留触发 halt
        assert ledger.halted is True
        with pytest.raises(BudgetExceededError):
            ledger.reserve("chat", "qwen3.8-max", 1)
        ledger.clear_halt("PG16 人工核查放行")
        assert ledger.halted is False
        ledger.verify_consistency()
    finally:
        ledger.close()

    # PG16 历史 JSONL 对照迁移
    jsonl_path = tmp_path / "pg_hist.jsonl"
    jsonl_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "reserved", "id": "pg-h1", "kind": "chat", "model": "qwen3.8-max", "estimate": 200}),
                json.dumps({"type": "committed", "id": "pg-h1", "model": "qwen3.8-max", "estimate": 200, "actual": 140}),
                json.dumps({"type": "reserved", "id": "pg-h2", "kind": "chat", "model": "qwen3.8-max", "estimate": 150}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    mig_scope = f"pg16-mig-{os.getpid()}"
    r_mig = migrate_jsonl_to_db(jsonl_path, limit=5000, model_limits={"qwen3.8-max": 5000}, scope=mig_scope, database_url=pg_url)
    assert r_mig["verified"] is True and r_mig["unsettled_ids"] == ["pg-h2"]
    r_mig2 = migrate_jsonl_to_db(jsonl_path, limit=5000, model_limits={"qwen3.8-max": 5000}, scope=mig_scope, database_url=pg_url)
    assert r_mig2["verified"] is True and r_mig2["inserted_reservations"] == 0

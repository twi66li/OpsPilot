"""T18：统一入口预算门控——账本持久/恢复/fail-closed/halt/分模型授权/单写者与发送前拒绝的 Mock 证明。"""
import json
from types import SimpleNamespace

import httpx
import pytest

from app.agent.llm import OpenAIChatLLM
from app.agent.tool_loop import run_tool_loop
from app.config import Settings
from app.rag.embeddings import OpenAIEmbedding
from app.services import budget as budget_mod
from app.services.budget import (
    BudgetExceededError,
    BudgetLedger,
    BudgetLedgerError,
    estimate_upper_bound,
    get_budget_guard,
    reset_budget_guard,
)
from app.services.model_api import ChatAPI, RerankAPI

UNREACHABLE = "http://127.0.0.1:9/v1"
RERANK_URL = "http://127.0.0.1:9"


class _Chunk:
    def __init__(self, text):
        self.text = text


def _settings(tmp_path, limit=1_000_000, models=None, path_name="ledger.jsonl"):
    return Settings(
        _env_file=None,
        budget_ledger_path=str(tmp_path / path_name),
        budget_token_limit=limit,
        budget_model_limits=models if models is not None else {"test-model": limit},
    )


@pytest.fixture()
def guard(tmp_path, monkeypatch, request):
    """配置指向临时账本的预算门控（limit 可按用例覆盖；模型授权默认 test-model）。"""
    limit = getattr(request, "param", 1_000_000)
    reset_budget_guard()
    monkeypatch.setattr("app.config.get_settings", lambda: _settings(tmp_path, limit=limit))
    return get_budget_guard()


@pytest.fixture()
def ledger_path(guard):
    yield guard.path
    reset_budget_guard()


@pytest.fixture(autouse=True)
def _release_locks():
    """每条用例结束后释放全部进程锁，避免测试间互相占用。"""
    yield
    for instance in list(budget_mod._guards.values()):
        instance.close()
    reset_budget_guard()


def _fresh_ledger(path, limit=10_000, models=None):
    """关闭缓存实例并重建账本（模拟重启：旧进程退出释放锁）。"""
    cached = get_budget_guard()
    if cached is not None:
        cached.close()
    reset_budget_guard()
    return BudgetLedger(path, limit, models if models is not None else {"test-model": limit})


# ---- 可证明保守上界（字节级） ----

def test_estimate_is_byte_level_provable_bound():
    # 每个 token 至少占 1 个 UTF-8 字节 ⇒ token 数 ≤ 字节数；上界=载荷字节+输出+思考封顶
    assert estimate_upper_bound(["a" * 2000], output_cap=1536) == 2000 + 1536
    assert estimate_upper_bound(["你好世界"], output_cap=0) == 12  # 4 CJK × 3 字节
    assert estimate_upper_bound(["a"], output_cap=1536) < estimate_upper_bound(["a"], output_cap=3000)
    with pytest.raises(BudgetExceededError):
        estimate_upper_bound(["x"], output_cap=None)


def test_over_reserve_keeps_actual_and_halts(tmp_path):
    """终审 F2 同型：实报超预留时保留原始实报并停机（字节上界也可能被越界 provider 超出）。"""
    ledger = BudgetLedger(str(tmp_path / "l.jsonl"), 10_000, {"test-model": 10_000})
    rid = ledger.reserve("chat", "test-model", 3536)  # 2000 字节载荷 + 1536 封顶
    ledger.settle(rid, {"total_tokens": 4500})
    assert ledger.usage_now()["actual"] == 4500, "实报全额落账"
    assert ledger.halted
    with pytest.raises(BudgetExceededError):
        ledger.reserve("chat", "test-model", 1)


# ---- F1 同型：真实入口无账本配置即 fail closed ----

@pytest.mark.parametrize("cls,kwargs", [
    (ChatAPI, {"api_key": "key", "base_url": UNREACHABLE, "model": "test-model"}),
    (RerankAPI, {"api_key": "key", "base_url": RERANK_URL, "model": "test-model"}),
    (OpenAIEmbedding, {"api_key": "key", "base_url": UNREACHABLE, "model": "test-model", "dim": 8}),
])
def test_real_entries_fail_closed_without_ledger_config(tmp_path, monkeypatch, cls, kwargs):
    monkeypatch.setattr("app.config.get_settings", lambda: Settings(_env_file=None))
    reset_budget_guard()
    with pytest.raises(BudgetLedgerError):
        cls(**kwargs)


# ---- 发送前拒绝（供应商零调用）：四通道同型证明 ----

@pytest.mark.parametrize("guard", [50], indirect=True)
def test_chat_over_limit_is_refused_before_reaching_supplier(ledger_path):
    api = ChatAPI("key", UNREACHABLE, "test-model")  # base_url 不可达：若触达会得到 ConnectionError
    with pytest.raises(BudgetExceededError):
        api.chat("你好", "当前状态如何")
    assert api.calls[-1]["status"] == "budget_refused"
    ledger = _fresh_ledger(ledger_path, limit=1_000_000)
    assert ledger.usage_now()["actual"] == 0, "被拒绝的调用不产生任何实报"


@pytest.mark.parametrize("guard", [50], indirect=True)
def test_tool_chat_over_limit_is_refused(ledger_path):
    api = ChatAPI("key", UNREACHABLE, "test-model")
    with pytest.raises(BudgetExceededError):
        api.tool_turn([{"role": "user", "content": "查询"}], [{"type": "function", "function": {"name": "f", "parameters": {}}}])
    assert api.calls[-1]["status"] == "budget_refused"


@pytest.mark.parametrize("guard", [50], indirect=True)
def test_rerank_over_limit_is_refused(ledger_path):
    api = RerankAPI("key", RERANK_URL, "test-model")
    with pytest.raises(BudgetExceededError):
        api.rerank("q", [_Chunk("文档文本")], top_k=3)
    assert api.calls[-1]["status"] == "budget_refused"


@pytest.mark.parametrize("guard", [5], indirect=True)
def test_embedding_over_limit_is_refused(ledger_path):
    api = OpenAIEmbedding("key", UNREACHABLE, "test-model", dim=8)
    with pytest.raises(BudgetExceededError):
        api.embed(["文本一", "text two"])
    assert api.calls[-1]["status"] == "budget_refused"


# ---- F3 同型：分模型授权与独立额度 ----

def test_unauthorized_model_is_refused_before_send(tmp_path):
    ledger = BudgetLedger(str(tmp_path / "l.jsonl"), 1_000_000, {"test-model": 1_000_000})
    with pytest.raises(BudgetExceededError):
        ledger.reserve("chat", "gpt-x-unauthorized", 10)


def test_per_model_cap_is_enforced_independently(tmp_path):
    """终审 F3 同型：单模型超其获批上限即拒；额度独立不串用。"""
    ledger = BudgetLedger(str(tmp_path / "l.jsonl"), 1_000_000, {"qwen3.8-max": 302500, "test-model": 1_000_000})
    with pytest.raises(BudgetExceededError):
        ledger.reserve("chat", "qwen3.8-max", 302501)
    rid = ledger.reserve("chat", "qwen3.8-max", 302500)
    ledger.settle(rid, None)  # 记 unknown 占满该模型额度
    with pytest.raises(BudgetExceededError):
        ledger.reserve("chat", "qwen3.8-max", 1)
    # 另一模型在其自身额度内不受前者影响（不自动串用总池）
    assert ledger.reserve("chat", "test-model", 100)


# ---- F4 同型：跨进程单写者 ----

def test_second_holder_of_same_path_is_refused(tmp_path):
    first = BudgetLedger(str(tmp_path / "l.jsonl"), 10_000, {"test-model": 10_000})
    try:
        with pytest.raises(BudgetLedgerError):
            BudgetLedger(str(tmp_path / "l.jsonl"), 10_000, {"test-model": 10_000})
    finally:
        first.close()
    # 持有者释放后可重新获取
    BudgetLedger(str(tmp_path / "l.jsonl"), 10_000, {"test-model": 10_000}).close()


# ---- F5 同型：请求 ID 唯一、结算精确关联 ----

def test_rids_are_unique_and_settlements_pair_exactly(tmp_path):
    ledger = BudgetLedger(str(tmp_path / "l.jsonl"), 10_000, {"test-model": 10_000})
    rid1 = ledger.reserve("chat", "test-model", 100)
    rid2 = ledger.reserve("chat", "test-model", 200)  # 同线程同毫秒
    assert rid1 != rid2
    ledger.settle(rid1, {"total_tokens": 150})  # 超预留 → halt
    assert ledger.halted
    ledger.settle(rid2, {"total_tokens": 200})  # 在途预留仍精确结算，不受 halt 影响
    with pytest.raises(BudgetLedgerError):
        ledger.settle("unknown-rid", None)
    with pytest.raises(BudgetLedgerError):
        ledger.settle(rid1, {"total_tokens": 1})  # 重复结算不静默覆盖
    with pytest.raises(BudgetExceededError):
        ledger.reserve("chat", "test-model", 1)  # halt 阻止新调用
    assert ledger.usage_now()["actual"] == 350
    ledger.close()
    rebuilt = BudgetLedger(str(tmp_path / "l.jsonl"), 10_000, {"test-model": 10_000})
    assert rebuilt.usage_now()["actual"] == 350, "重放后累计正确"


# ---- 预留/结算与账本恢复 ----

def test_missing_usage_is_settled_as_unknown_not_zero(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    ledger = BudgetLedger(path, 100_000, {"test-model": 100_000})
    rid = ledger.reserve("chat", "test-model", 500)
    ledger.settle(rid, None)  # 超时/缺 usage：不释放、不记零
    assert ledger.usage_now()["unknown"] == 500 and ledger.usage_now()["in_flight"] == 0
    ledger.close()
    rebuilt = BudgetLedger(path, 100_000, {"test-model": 100_000})
    assert rebuilt.usage_now()["unknown"] == 500


def test_unsettled_reservation_survives_restart(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    ledger = BudgetLedger(path, 10_000, {"test-model": 10_000})
    ledger.reserve("chat", "test-model", 30)  # 模拟中断遗留在途
    ledger.close()
    rebuilt = _fresh_ledger(path, limit=10_000)
    assert rebuilt.usage_now()["in_flight"] == 30
    rebuilt.reserve("chat", "test-model", 40)
    rebuilt.close()
    assert _fresh_ledger(path, limit=10_000).usage_now()["in_flight"] == 70, "新预留经再次恢复不得丢失"


def test_reserve_write_failure_is_fail_closed(tmp_path, monkeypatch):
    path = str(tmp_path / "ledger.jsonl")
    ledger = BudgetLedger(path, 10_000, {"test-model": 10_000})

    def broken_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(budget_mod, "open", broken_open, raising=False)
    with pytest.raises(BudgetLedgerError):
        ledger.reserve("chat", "test-model", 10)


def test_settlement_write_failure_halts_process(tmp_path, monkeypatch):
    path = str(tmp_path / "ledger.jsonl")
    ledger = BudgetLedger(path, 10_000, {"test-model": 10_000})
    rid = ledger.reserve("chat", "test-model", 10)
    monkeypatch.setattr(budget_mod, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")), raising=False)
    with pytest.raises(BudgetLedgerError):
        ledger.settle(rid, {"total_tokens": 12})
    with pytest.raises(BudgetExceededError):
        ledger.reserve("chat", "test-model", 1)
    assert ledger.halted, "结算失败后进程内立即停止后续调用"


def test_corrupt_trailing_line_fails_closed_and_preserves_bytes(tmp_path):
    """n1030-codex-008 F1 同型：残缺末行必须 fail closed，不得粘连追加。"""
    path = tmp_path / "ledger.jsonl"
    good = {"type": "reserved", "id": "r1", "at": "t", "pid": 1, "kind": "chat", "model": "test-model", "estimate": 30}
    with open(path, "wb") as f:
        f.write((json.dumps(good, ensure_ascii=False) + "\n").encode())
        f.write(b'{"type":"committed","id":"r1","estima')  # 无换行的截断尾行
    raw_before = path.read_bytes()

    ledger = BudgetLedger(str(path), 10_000, {"test-model": 10_000})
    assert ledger.corrupt
    with pytest.raises(BudgetLedgerError):
        ledger.reserve("chat", "test-model", 1)
    assert path.read_bytes() == raw_before, "fail closed 不得改写或粘连原始账本"
    ledger.close()

    # 人工修复尾行为完整记录后，账本恢复可用且原预留计入
    with open(path, "ab") as f:
        f.write(b'te":30,"actual":20,"over_reserve":false}\n')
    repaired = _fresh_ledger(str(path), limit=10_000)
    assert repaired.usage_now()["actual"] == 20 and repaired.usage_now()["in_flight"] == 0
    assert repaired.reserve("chat", "test-model", 10)


def test_invalid_middle_line_fails_closed(tmp_path):
    path = tmp_path / "ledger.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"type":"reserved","id":"r1","model":"test-model","estimate":30}\n')
        f.write("not-json\n")
        f.write('{"type":"reserved","id":"r2","model":"test-model","estimate":40}\n')
    ledger = BudgetLedger(str(path), 10_000, {"test-model": 10_000})
    assert ledger.corrupt
    with pytest.raises(BudgetLedgerError):
        ledger.reserve("chat", "test-model", 1)


# ---- F3(halt) 同型：实报超预留、随账本恢复、显式解除 ----

def test_over_reserve_halt_persists_across_rebuild(tmp_path):
    """n1030-codex-008 F3 同型：halt 必须随权威账本恢复，普通重建不是人工核查。"""
    path = str(tmp_path / "ledger.jsonl")
    ledger = BudgetLedger(path, 1000, {"test-model": 1000})
    rid = ledger.reserve("chat", "test-model", 10)
    ledger.settle(rid, {"total_tokens": 20})
    assert ledger.halted
    ledger.close()

    rebuilt = _fresh_ledger(path, limit=1000)  # 不做任何人工核查直接重建
    assert rebuilt.halted, "halt 必须由账本重放恢复"
    assert rebuilt.usage_now()["actual"] == 20, "实报全额保留"
    with pytest.raises(BudgetExceededError):
        rebuilt.reserve("chat", "test-model", 1)

    rebuilt.clear_halt("人工核查：确认超预留为指标统计口径差，授权继续")
    rebuilt.close()
    assert BudgetLedger(path, 1000, {"test-model": 1000}).reserve("chat", "test-model", 1)


# ---- F2 同型：预算/账本错误不被非 strict Mock 回退掩盖 ----

def _corrupt_ledger_settings(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(b'{"type":"reserved","id":"r1","estima')  # 残缺（无换行）
    return Settings(_env_file=None, budget_ledger_path=str(path), budget_token_limit=100_000,
                    budget_model_limits={"test-model": 100_000})


@pytest.mark.parametrize("method", ["generate_answer", "draft_ticket", "is_sufficient", "rewrite_query"])
def test_budget_ledger_errors_are_not_masked_by_mock_fallback(tmp_path, monkeypatch, method):
    reset_budget_guard()
    monkeypatch.setattr("app.config.get_settings", lambda: _corrupt_ledger_settings(tmp_path))
    llm = OpenAIChatLLM("key", UNREACHABLE, "test-model", strict=False)
    api = llm._api
    calls = {
        "generate_answer": lambda: llm.generate_answer("问题", [], [], [], []),
        "draft_ticket": lambda: llm.draft_ticket("问题", "回答"),
        "is_sufficient": lambda: llm.is_sufficient("问题", [{"doc_title": "t", "text": "x"}]),
        "rewrite_query": lambda: llm.rewrite_query("问题"),
    }
    with pytest.raises(BudgetLedgerError):
        calls[method]()
    assert all(c.get("status") == "budget_refused" for c in api.calls), "供应商零调用"


def test_budget_exceeded_propagates_through_non_strict_llm(tmp_path, monkeypatch):
    reset_budget_guard()
    monkeypatch.setattr("app.config.get_settings",
                        lambda: _settings(tmp_path, limit=10, models={"test-model": 10}))
    llm = OpenAIChatLLM("key", UNREACHABLE, "test-model", strict=False)
    with pytest.raises(BudgetExceededError):
        llm.generate_answer("问题", [], [], [], [])


def test_tool_loop_propagates_budget_stop():
    class _BudgetStoppedAPI:
        def tool_turn(self, messages, tools, *, allow_tools=True):
            raise BudgetExceededError("预算超限")

    with pytest.raises(BudgetExceededError):
        run_tool_loop("问题", _BudgetStoppedAPI(), lambda name, args: {"status": "ok"})


# ---- 全层重试合计初次+1 ----

def test_eval_judge_retry_is_capped_at_one_extra_attempt(tmp_path):
    import inspect

    from eval.judge_sensitivity import Audit, score_one

    assert inspect.signature(score_one).parameters["max_retries"].default == 1

    class _FlakyAPI:
        def __init__(self):
            self.calls = []
            self.client = SimpleNamespace(close=lambda: None)

        def chat(self, system, user, *, json_mode=False):
            self.calls.append(1)
            raise httpx.ConnectError("网络故障", request=None)

    answer = {"id": "q1", "task_type": "qa", "mode": "rule", "judgment": {}, "answer": "回答"}
    question = {"question": "q", "reference_answer": "参考", "answer_points": ["要点"], "should_abstain": False}
    result = score_one(question, answer, {"hits": []}, "rubric", _FlakyAPI(), Audit(tmp_path / "audit.jsonl"))
    assert len(result["attempt_errors"]) == 2, "初次+最多1次额外重试，共2次尝试"


# ---- 门控启用与单写者缓存 ----

def test_guard_requires_positive_limit_and_model_grants(tmp_path, monkeypatch):
    reset_budget_guard()
    monkeypatch.setattr("app.config.get_settings",
                        lambda: _settings(tmp_path, limit=0))
    with pytest.raises(BudgetLedgerError):
        get_budget_guard()
    reset_budget_guard()
    monkeypatch.setattr("app.config.get_settings",
                        lambda: _settings(tmp_path, limit=100, models={}))
    with pytest.raises(BudgetLedgerError):
        get_budget_guard()


def test_same_path_shares_single_writer_instance(tmp_path, monkeypatch):
    reset_budget_guard()
    monkeypatch.setattr("app.config.get_settings", lambda: _settings(tmp_path))
    assert get_budget_guard() is get_budget_guard()
    assert len(budget_mod._guards) == 1


def test_unset_path_disables_guard(monkeypatch):
    reset_budget_guard()
    monkeypatch.setattr("app.config.get_settings", lambda: Settings(_env_file=None))
    assert get_budget_guard() is None


# ---- 成功/失败调用按实际 usage 结算（缺 usage 记 unknown 的对偶面） ----

def test_error_call_is_settled_as_unknown(tmp_path, monkeypatch):
    """模型调用失败（不可达）后，预留按 unknown 结算，不按零费用释放。"""
    reset_budget_guard()
    settings = _settings(tmp_path, limit=1_000_000)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    api = ChatAPI("key", UNREACHABLE, "test-model")
    with pytest.raises(Exception):
        api.chat("你好", "世界")
    expected = estimate_upper_bound(["你好", "世界"], output_cap=1536)
    api._budget.close()
    rebuilt = _fresh_ledger(settings.budget_ledger_path, limit=1_000_000)
    usage = rebuilt.usage_now()
    assert usage["unknown"] == expected, "失败调用按保守预留记 unknown，不按零费用释放"
    assert usage["actual"] == 0
    assert usage["models"]["test-model"]["unknown"] == expected, "分模型记账与总额一致"

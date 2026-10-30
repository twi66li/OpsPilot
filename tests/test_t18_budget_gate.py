"""T18：统一入口预算门控——账本持久/恢复/fail-closed/halt 与全通道发送前拒绝的 Mock 证明。"""
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


@pytest.fixture()
def guard(tmp_path, monkeypatch, request):
    """配置指向临时账本的预算门控（limit 可按用例覆盖）。"""
    limit = getattr(request, "param", 1_000_000)
    reset_budget_guard()
    settings = Settings(_env_file=None, budget_ledger_path=str(tmp_path / "ledger.jsonl"), budget_token_limit=limit)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    return get_budget_guard()


@pytest.fixture()
def ledger_path(guard):
    path = guard.path
    yield path
    reset_budget_guard()


# ---- 保守上界 ----

def test_estimate_is_conservative_for_cjk_and_latin_and_caps_output():
    estimate = estimate_upper_bound(["你好世界", "abcdefghij"], output_cap=1536, thinking_cap=0)
    assert estimate >= 4 + 4 + 1536  # CJK 按字、英文按 3 字符 1 token 的保守上界 + 输出封顶
    assert estimate_upper_bound(["abc"], output_cap=1536) < estimate_upper_bound(["abc"], output_cap=3000)


def test_call_without_output_bound_is_rejected_before_send():
    with pytest.raises(BudgetExceededError):
        estimate_upper_bound(["x"], output_cap=None)


# ---- 发送前拒绝（供应商零调用）：三个通道同型证明 ----

@pytest.mark.parametrize("guard", [50], indirect=True)
def test_chat_over_limit_is_refused_before_reaching_supplier(ledger_path):
    api = ChatAPI("key", UNREACHABLE, "test-model")  # base_url 不可达：若触达会得到 ConnectionError
    with pytest.raises(BudgetExceededError):
        api.chat("你好", "当前状态如何")
    assert api.calls[-1]["status"] == "budget_refused"
    ledger = BudgetLedger(ledger_path, 1_000_000)
    assert ledger.usage_now()["actual"] == 0, "被拒绝的调用不产生任何实报"


@pytest.mark.parametrize("guard", [50], indirect=True)
def test_tool_chat_over_limit_is_refused(ledger_path):
    api = ChatAPI("key", UNREACHABLE, "test-model")
    with pytest.raises(BudgetExceededError):
        api.tool_turn([{"role": "user", "content": "查询"}], [{"type": "function", "function": {"name": "f", "parameters": {}}}])
    assert api.calls[-1]["status"] == "budget_refused"


@pytest.mark.parametrize("guard", [50], indirect=True)
def test_rerank_over_limit_is_refused(ledger_path):
    api = RerankAPI("key", RERANK_URL, "test-reranker")
    with pytest.raises(BudgetExceededError):
        api.rerank("q", [_Chunk("文档文本")], top_k=3)
    assert api.calls[-1]["status"] == "budget_refused"


@pytest.mark.parametrize("guard", [5], indirect=True)
def test_embedding_over_limit_is_refused(ledger_path):
    api = OpenAIEmbedding("key", UNREACHABLE, "test-embedding", dim=8)
    with pytest.raises(BudgetExceededError):
        api.embed(["文本一", "text two"])
    assert api.calls[-1]["status"] == "budget_refused"


# ---- 预留/结算与账本恢复 ----

def test_missing_usage_is_settled_as_unknown_not_zero(tmp_path):
    reset_budget_guard()
    path = str(tmp_path / "ledger.jsonl")
    ledger = BudgetLedger(path, 100_000)
    rid = ledger.reserve("chat", "test-model", 500)
    ledger.settle(rid, None)  # 超时/缺 usage：不释放、不记零
    assert ledger.usage_now()["unknown"] == 500 and ledger.usage_now()["in_flight"] == 0
    rebuilt = BudgetLedger(path, 100_000)
    assert rebuilt.usage_now()["unknown"] == 500


def test_unsettled_reservation_survives_restart(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    ledger = BudgetLedger(path, 10_000)
    ledger.reserve("chat", "test-model", 30)  # 模拟中断遗留在途
    rebuilt = BudgetLedger(path, 10_000)
    assert rebuilt.usage_now()["in_flight"] == 30
    rebuilt.reserve("chat", "test-model", 40)
    assert BudgetLedger(path, 10_000).usage_now()["in_flight"] == 70, "新预留经再次恢复不得丢失"


def test_reserve_write_failure_is_fail_closed(tmp_path, monkeypatch):
    path = str(tmp_path / "ledger.jsonl")
    ledger = BudgetLedger(path, 10_000)

    def broken_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(budget_mod, "open", broken_open, raising=False)
    with pytest.raises(BudgetLedgerError):
        ledger.reserve("chat", "test-model", 10)


def test_settlement_write_failure_halts_process(tmp_path, monkeypatch):
    path = str(tmp_path / "ledger.jsonl")
    ledger = BudgetLedger(path, 10_000)
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
    good = {"type": "reserved", "id": "r1", "at": "t", "pid": 1, "kind": "chat", "model": "m", "estimate": 30}
    with open(path, "wb") as f:
        f.write((json.dumps(good, ensure_ascii=False) + "\n").encode())
        f.write(b'{"type":"committed","id":"r1","estima')  # 无换行的截断尾行
    raw_before = path.read_bytes()

    ledger = BudgetLedger(str(path), 10_000)
    assert ledger.corrupt
    with pytest.raises(BudgetLedgerError):
        ledger.reserve("chat", "test-model", 1)
    assert path.read_bytes() == raw_before, "fail closed 不得改写或粘连原始账本"

    # 人工修复尾行为完整记录后，账本恢复可用且原预留计入
    with open(path, "ab") as f:
        f.write(b'te":30,"actual":20,"over_reserve":false}\n')
    repaired = BudgetLedger(str(path), 10_000)
    assert repaired.usage_now()["actual"] == 20 and repaired.usage_now()["in_flight"] == 0
    assert repaired.reserve("chat", "test-model", 10)


def test_invalid_middle_line_fails_closed(tmp_path):
    path = tmp_path / "ledger.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"type":"reserved","id":"r1","estimate":30}\n')
        f.write("not-json\n")
        f.write('{"type":"reserved","id":"r2","estimate":40}\n')
    ledger = BudgetLedger(str(path), 10_000)
    assert ledger.corrupt
    with pytest.raises(BudgetLedgerError):
        ledger.reserve("chat", "test-model", 1)


# ---- halt：实报超预留、随账本恢复、显式解除 ----

def test_over_reserve_halt_persists_across_rebuild(tmp_path):
    """n1030-codex-008 F3 同型：halt 必须随权威账本恢复，普通重建不是人工核查。"""
    path = str(tmp_path / "ledger.jsonl")
    ledger = BudgetLedger(path, 1000)
    rid = ledger.reserve("chat", "test-model", 10)
    ledger.settle(rid, {"total_tokens": 20})
    assert ledger.halted
    with pytest.raises(BudgetExceededError):
        ledger.reserve("chat", "test-model", 1)

    rebuilt = BudgetLedger(path, 1000)  # 不做任何人工核查直接重建
    assert rebuilt.halted, "halt 必须由账本重放恢复"
    assert rebuilt.usage_now()["actual"] == 20, "实报全额保留"
    with pytest.raises(BudgetExceededError):
        rebuilt.reserve("chat", "test-model", 1)

    rebuilt.clear_halt("人工核查：确认超预留为指标统计口径差，授权继续")
    assert BudgetLedger(path, 1000).reserve("chat", "test-model", 1)


# ---- F2 同型：预算/账本错误不被非 strict Mock 回退掩盖 ----

def _corrupt_ledger_settings(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(b'{"type":"reserved","id":"r1","estima')  # 残缺（无换行）
    return Settings(_env_file=None, budget_ledger_path=str(path), budget_token_limit=100_000)


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
    settings = Settings(_env_file=None, budget_ledger_path=str(tmp_path / "l.jsonl"), budget_token_limit=10)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
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


# ---- 门控启用与单写者 ----

def test_guard_requires_positive_limit(tmp_path, monkeypatch):
    reset_budget_guard()
    settings = Settings(_env_file=None, budget_ledger_path=str(tmp_path / "l.jsonl"), budget_token_limit=0)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    with pytest.raises(BudgetLedgerError):
        get_budget_guard()


def test_same_path_shares_single_writer_instance(tmp_path, monkeypatch):
    reset_budget_guard()
    settings = Settings(_env_file=None, budget_ledger_path=str(tmp_path / "l.jsonl"), budget_token_limit=100)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    assert get_budget_guard() is get_budget_guard()
    assert budget_mod._guards and len(budget_mod._guards) == 1


def test_unset_path_disables_guard(monkeypatch):
    reset_budget_guard()
    monkeypatch.setattr("app.config.get_settings", lambda: Settings(_env_file=None))
    assert get_budget_guard() is None


# ---- 成功调用按实际 usage 结算（缺 usage 记 unknown 的对偶面） ----

def test_error_call_is_settled_as_unknown(tmp_path):
    """模型调用失败（不可达）后，预留按 unknown 结算，不按零费用释放。"""
    reset_budget_guard()
    path = str(tmp_path / "ledger.jsonl")
    monkey_settings = Settings(_env_file=None, budget_ledger_path=path, budget_token_limit=1_000_000)

    import app.config as config_mod

    original = config_mod.get_settings
    config_mod.get_settings = lambda: monkey_settings
    reset_budget_guard()
    try:
        api = ChatAPI("key", UNREACHABLE, "test-model")
        with pytest.raises(Exception):
            api.chat("你好", "世界")
    finally:
        config_mod.get_settings = original
    rebuilt = BudgetLedger(path, 1_000_000)
    usage = rebuilt.usage_now()
    assert usage["unknown"] >= 1536, "失败调用按保守预留记 unknown，不按零费用释放"
    assert usage["actual"] == 0

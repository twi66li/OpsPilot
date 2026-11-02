"""P1-Q-SWITCH 离线验证：本地假账本 + 录制异常，验证工作台真实调用链的候选切换。

覆盖：
1. 首选模型分模型预留不足 → 切至候选并成功，切换记入 calls 审计；
2. 认证错误（401）不误切；
3. 项目总额不足不切换（不能借换模型突破总额）；
4. 供应商 429 同模型额外重试一次。
不触达任何真实供应商；不访问模型、数据库。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

TMP = Path(__file__).parent / "_switch_test"
TMP.mkdir(exist_ok=True)
LEDGER = TMP / "ledger.jsonl"
for f in (LEDGER, Path(str(LEDGER) + ".lock")):
    if f.exists():
        f.unlink()

os.environ["BUDGET_LEDGER_PATH"] = str(LEDGER)
os.environ["BUDGET_TOKEN_LIMIT"] = "100000"
os.environ["BUDGET_MODEL_LIMITS"] = json.dumps({
    "primary-fake": 50,        # 首选仅够 1 次预留，之后分模型不足 → 触发切换
    "backup-fake": 50000,      # 候选充足
    "total-fake": 50000,
    "auth-fake": 50000,        # 401 测试独立模型（避免共用账本额度先于 401 触发预留拒绝）
})
os.environ["CHAT_MODEL_CANDIDATES"] = json.dumps(["backup-fake"])

from openai import APIStatusError  # noqa: E402
import httpx  # noqa: E402

from app.services.budget import BudgetExceededError, reset_budget_guard  # noqa: E402
reset_budget_guard()
from app.services.model_api import ChatAPI  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


class FakeCompletions:
    """录制式假客户端：按模型返回成功或抛指定异常。"""

    def __init__(self, behavior):
        self.behavior = behavior  # {model: callable(attempt)->response-like or raise}
        self.attempts = []

    def create(self, *, model, **kwargs):
        self.attempts.append(model)
        fn = self.behavior[model]
        return fn(len([a for a in self.attempts if a == model]))


class _FakeStream:
    """stream=True 所需的最小响应对象（dunder 必须定义在类型上）。"""

    def __init__(self, text, usage):
        import types

        self._chunk = types.SimpleNamespace(
            usage=types.SimpleNamespace(model_dump=lambda: usage),
            choices=[types.SimpleNamespace(delta=types.SimpleNamespace(content=text),
                                           finish_reason="stop")])

    def __enter__(self):
        return iter([self._chunk])

    def __exit__(self, *a):
        return False


def fake_stream(text, usage):
    return _FakeStream(text, usage)


def main():
    api = ChatAPI("fake-key", "https://fake.aliyuncs.com/compatible-mode/v1", "primary-fake")

    # 1) 首选分模型预留不足 → 切至 backup-fake 成功
    api.client.chat.completions = FakeCompletions({
        "primary-fake": lambda n: (_ for _ in ()).throw(BudgetExceededError(
            "模型 primary-fake 额度超限：实报 40 + 未知 0 + 在途 0 + 本次预估 200 超过授权 50，请求在到达供应商前被拒绝")),
        "backup-fake": lambda n: fake_stream("候选成功", {"total_tokens": 30}),
    })
    text = api.chat("sys", "user")
    switches = [c for c in api.calls if c.get("status") == "model_switch"]
    check("分模型预留不足切至候选并成功", text == "候选成功" and len(switches) == 1
          and switches[0]["to"] == "backup-fake", f"switches={switches}")

    # 2) 认证错误（401）不误切
    def _401(n):
        raise APIStatusError("401 unauthorized", response=httpx.Response(401, request=httpx.Request("POST", "http://fake")),
                             body=None)
    api2 = ChatAPI("fake-key", "https://fake.aliyuncs.com/compatible-mode/v1", "auth-fake")
    api2.client.chat.completions = FakeCompletions({"auth-fake": _401, "backup-fake": lambda n: fake_stream("x", {})})
    raised = False
    try:
        api2.chat("sys", "user")
    except APIStatusError:
        raised = True
    switched = [c for c in api2.calls if c.get("status") == "model_switch"]
    check("认证错误（401）不误切、直接抛出", raised and not switched, f"switches={switched}")

    # 3) 项目总额不足不切换
    def _total(n):
        raise BudgetExceededError("预算超限：实报 90000 + 未知 0 + 在途 0 + 本次预估 200 = 90200 超过总授权 100000，"
                                  "请求在到达供应商前被拒绝")
    api3 = ChatAPI("fake-key", "https://fake.aliyuncs.com/compatible-mode/v1", "backup-fake")
    api3.client.chat.completions = FakeCompletions({"backup-fake": _total, "primary-fake": lambda n: fake_stream("x", {})})
    raised_total = False
    try:
        api3.chat("sys", "user")
    except BudgetExceededError as exc:
        raised_total = "超过总授权" in str(exc)
    switched3 = [c for c in api3.calls if c.get("status") == "model_switch"]
    check("项目总额不足不切换（不能借换模型突破总额）", raised_total and not switched3, f"switches={switched3}")

    # 4) 供应商 429 同模型额外重试一次
    api4 = ChatAPI("fake-key", "https://fake.aliyuncs.com/compatible-mode/v1", "backup-fake")
    calls4 = {"n": 0}

    def _429_then_ok(n):
        calls4["n"] += 1
        if n == 1:
            raise APIStatusError("429 rate limited", response=httpx.Response(429, request=httpx.Request("POST", "http://fake")),
                                 body=None)
        return fake_stream("重试成功", {"total_tokens": 25})
    api4.client.chat.completions = FakeCompletions({"backup-fake": _429_then_ok})
    text4 = api4.chat("sys", "user")
    retries = [c for c in api4.calls if c.get("status") == "transient_retry"]
    check("供应商 429 同模型额外重试一次并成功", text4 == "重试成功" and calls4["n"] == 2 and len(retries) == 1,
          f"provider_attempts={calls4['n']} retries={len(retries)}")

    fails = [n for n, ok, _ in RESULTS if not ok]
    print(f"SWITCH VERIFY: {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS" + (f" FAILURES={fails}" if fails else ""))


if __name__ == "__main__":
    main()

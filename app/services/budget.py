"""统一入口预算门控：所有真实模型调用通道（chat/tool_chat、Embedding、Rerank）共用。

设计要点（本夜 api_gate 条款）：
- 预留先行：任何真实请求发送前必须 reserve 成功，超限/写失败都在到达供应商前拒绝；
- 权威 JSONL 账本：reserved / committed / committed_unknown / clearance 逐行追加，
  重启重放恢复 实报(actual) / 未知(unknown) / 在途(in_flight)，超时或缺 usage 记
  unknown 而不是零费用；残缺或无效行整体 fail closed 并保留原文，不粘连不丢账；
- halt：实报超预留（committed over_reserve）后停止后续调用，halt 随账本重放恢复，
  只有显式 clearance 记录（人工核查）才能解除，普通重建不等于人工核查；
- 单写者：同进程同一路径共享同一实例（模块级缓存 + RLock），账本每行记录 pid 供
  跨进程审计；约束为单进程写账本，评测与 API 不同时写同一路径。
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """预算超限或 halt：请求在到达供应商之前被拒绝。"""


class BudgetLedgerError(Exception):
    """预算账本/门控基础设施失败：fail closed，必须显式传播，不得回退 Mock。"""


def estimate_upper_bound(texts, *, output_cap: int, thinking_cap: int = 0) -> int:
    """保守输入上界 + 输出/思考封顶。

    中文按 1 字 1 token，非 CJK 按 3 字符 1 token（宽于常见的 4 字符 1 token），
    覆盖 UTF-8 多字节与非 CJK 文本；输出封顶来自调用参数（max_tokens），
    thinking_cap 为思考量上界（DashScope enable_thinking=False 记 0）。
    无法给出输出封顶的调用方必须传 None 并在发送前被拒绝。
    """
    if output_cap is None or output_cap < 0:
        raise BudgetExceededError("调用无法保证输出上界，发送前拒绝")
    total = 0
    for text in texts:
        if not text:
            continue
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        rest = len(text) - cjk
        total += cjk + -(-rest // 3)
    return total + output_cap + thinking_cap


def _total_tokens(usage) -> int | None:
    """按接口定义取总 token：优先 total_tokens，避免与分项重复叠加；缺 usage 视为未知。"""
    if not isinstance(usage, dict) or not usage:
        return None
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and total > 0:
        return int(total)
    parts = sum(v for k, v in usage.items() if isinstance(v, (int, float)) and k.endswith("tokens") and k != "total_tokens")
    return int(parts) if parts > 0 else None


class BudgetLedger:
    """单路径 JSONL 权威账本：预留/结算/halt/解除，重启可恢复。"""

    def __init__(self, path: str, limit: int):
        if limit <= 0:
            raise BudgetLedgerError("预算上限必须为正，拒绝在无上限时放行真实调用")
        self.path = path
        self.limit = limit
        self.actual = 0
        self.unknown = 0
        self.in_flight = 0
        self.halted = False
        self.halted_reason = ""
        self.corrupt = False
        self.corrupt_detail = ""
        self._lock = threading.RLock()
        self._pending: dict[str, int] = {}
        self._replay()

    # ---- 账本重放 ----
    def _replay(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as f:
            raw = f.read()
        if raw and not raw.endswith(b"\n"):
            self._fail_corrupt("末行未正常终止（可能写入中断）")
            return
        text = raw.decode("utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                self._apply(record)
            except BudgetLedgerError:
                raise
            except Exception as exc:
                self._fail_corrupt(f"第{lineno}行无效：{type(exc).__name__}")
                return

    def _apply(self, record):
        kind = record.get("type")
        if kind == "reserved":
            self.in_flight += int(record["estimate"])
        elif kind == "committed":
            estimate, actual = int(record["estimate"]), int(record["actual"])
            self.in_flight -= estimate
            self.actual += actual
            if actual > estimate:
                self.halted = True
                self.halted_reason = f"实报 {actual} 超预留 {estimate}（记录 {record.get('id')}）"
        elif kind == "committed_unknown":
            estimate = int(record["estimate"])
            self.in_flight -= estimate
            self.unknown += estimate
        elif kind == "clearance":
            self.halted = False
            self.halted_reason = ""
        else:
            raise ValueError(f"未知账本记录类型 {kind!r}")

    def _fail_corrupt(self, detail: str):
        self.corrupt = True
        self.corrupt_detail = detail
        self.actual = self.unknown = self.in_flight = 0
        self.halted = True
        self.halted_reason = f"账本损坏：{detail}"

    # ---- 写入 ----
    def _append(self, record: dict):
        line = json.dumps(record, ensure_ascii=False) + "\n"
        try:
            with open(self.path, "ab+") as f:
                # ab+ 仍保证写入始终追加；读取末字节校验上一行是否正常终止
                if f.tell() > 0:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        raise BudgetLedgerError("账本末行未终止，追加会粘连丢账，fail closed")
                    f.seek(0, os.SEEK_END)
                f.write(line.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
        except BudgetLedgerError:
            raise
        except OSError as exc:
            raise BudgetLedgerError(f"账本写入失败：{type(exc).__name__}") from exc

    @staticmethod
    def _stamp():
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    # ---- 对外接口 ----
    def reserve(self, kind: str, model: str, estimate: int) -> str:
        """发送前预留；超限/损坏/写失败都在此拒绝，调用方不得触达供应商。"""
        with self._lock:
            if self.corrupt:
                raise BudgetLedgerError(f"账本损坏（{self.corrupt_detail}），fail closed，请人工核查账本")
            if self.halted:
                raise BudgetExceededError(f"预算已停止：{self.halted_reason}；需人工核查后写入 clearance 才能恢复")
            used = self.actual + self.unknown + self.in_flight
            if used + estimate > self.limit:
                raise BudgetExceededError(
                    f"预算超限：实报 {self.actual} + 未知 {self.unknown} + 在途 {self.in_flight} + 本次预估 {estimate}"
                    f" = {used + estimate} 超过授权 {self.limit}，请求在到达供应商前被拒绝"
                )
            rid = f"r{int(time.time() * 1000):x}-{os.getpid()}-{id(threading.current_thread()) % 99999:x}"
            self._append({"type": "reserved", "id": rid, "at": self._stamp(), "pid": os.getpid(),
                          "kind": kind, "model": model, "estimate": estimate})
            self.in_flight += estimate
            self._pending[rid] = estimate
            return rid

    def settle(self, rid: str, usage) -> None:
        """结算：usage 缺失/超时记 unknown（不释放、不记零）；实报超预留全额入账并 halt。

        结算写失败时账本无法记录本次实报，进程内立即 halt（fail closed）；
        预留行保持未结算，重启重放按 unknown 计入，不会少记。
        """
        with self._lock:
            estimate = self._pending.pop(rid, None)
            if estimate is None:
                raise BudgetLedgerError(f"结算找不到预留 {rid}")
            try:
                actual = _total_tokens(usage)
                if actual is None:
                    self._append({"type": "committed_unknown", "id": rid, "at": self._stamp(), "pid": os.getpid(),
                                  "estimate": estimate})
                    self.in_flight -= estimate
                    self.unknown += estimate
                    return
                over = actual > estimate
                self._append({"type": "committed", "id": rid, "at": self._stamp(), "pid": os.getpid(),
                              "estimate": estimate, "actual": actual, "over_reserve": over})
                self.in_flight -= estimate
                self.actual += actual
                if over:
                    self.halted = True
                    self.halted_reason = f"实报 {actual} 超预留 {estimate}（记录 {rid}），需人工核查"
            except BudgetLedgerError:
                self.halted = True
                self.halted_reason = "结算写入失败，账本 fail closed，需人工核查"
                raise

    def clear_halt(self, note: str) -> None:
        """人工核查后显式解除 halt；写失败则保持 halt。"""
        with self._lock:
            if not self.halted:
                return
            self._append({"type": "clearance", "at": self._stamp(), "pid": os.getpid(), "note": note})
            self.halted = False
            self.halted_reason = ""

    def usage_now(self) -> dict:
        with self._lock:
            return {"actual": self.actual, "unknown": self.unknown, "in_flight": self.in_flight,
                    "limit": self.limit, "halted": self.halted, "corrupt": self.corrupt}


_guards: dict[str, BudgetLedger] = {}
_guard_lock = threading.Lock()


def reset_budget_guard() -> None:
    """测试用：清空单写者缓存。"""
    with _guard_lock:
        _guards.clear()


def get_budget_guard():
    """按 settings 返回进程内单例账本；未配置路径返回 None（离线 Mock 不记账）。"""
    from app.config import get_settings

    settings = get_settings()
    path = getattr(settings, "budget_ledger_path", "")
    if not path:
        return None
    with _guard_lock:
        guard = _guards.get(path)
        if guard is None:
            limit = int(getattr(settings, "budget_token_limit", 0) or 0)
            if limit <= 0:
                raise BudgetLedgerError("配置了预算账本路径但 budget_token_limit 非正，拒绝放行真实调用")
            guard = BudgetLedger(path, limit)
            _guards[path] = guard
        return guard

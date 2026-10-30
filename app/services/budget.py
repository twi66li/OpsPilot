"""统一入口预算门控：所有真实模型调用通道（chat/tool_chat、Embedding、Rerank）共用。

设计要点（本夜 api_gate 条款 + 终审 findings 修复）：
- 预留先行：真实供应商入口构造即要求已配置持久账本，无配置 fail closed；
  任何请求发送前必须 reserve 成功，超限/未授权模型/写失败都在到达供应商前拒绝；
- 可证明保守上界：字节级分词器（含 Qwen BPE）每个 token 至少占 1 字节，
  因此 token 数 ≤ 请求载荷 UTF-8 字节数；上界 = 载荷字节数 + 输出封顶 + 思考封顶，
  载荷覆盖全部消息内容、历史 tool_calls 与工具 schema；输出封顶缺失直接拒绝；
- 分模型授权：按 budget.json p1 列逐模型校验，未授权模型拒绝，各模型
  实报+未知+在途独立计数、不超其获批上限，不做统一总池自动串用；
- 权威 JSONL 账本：reserved / committed / committed_unknown / clearance 逐行追加
  并 fsync，重启重放恢复 实报(actual) / 未知(unknown) / 在途(in_flight)；超时或缺
  usage 记 unknown 而不是零费用；残缺或无效行整体 fail closed 并保留原文；
- halt：实报超预留后停止后续调用（保留原始实报），halt 随账本重放恢复，只有
  显式 clearance 记录（人工核查）才能解除，普通重建不等于人工核查；
- 单写者：进程内同路径共享单实例（模块缓存+RLock）；跨进程用 OS 级锁文件
  （fcntl/msvcrt，进程退出自动释放），第二持有者构造即被拒绝。
"""
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone

try:
    import fcntl  # POSIX 进程锁
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt  # Windows 进程锁
except ImportError:  # POSIX
    msvcrt = None

logger = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """预算超限/未授权模型/halt：请求在到达供应商之前被拒绝。"""


class BudgetLedgerError(Exception):
    """预算账本/门控基础设施失败：fail closed，必须显式传播，不得回退 Mock。"""


def estimate_upper_bound(texts, *, output_cap, thinking_cap: int = 0) -> int:
    """可证明保守输入上界 + 输出/思考封顶。

    依据模型定义：Qwen 系列为字节级 BPE，每个 token 至少编码 1 个 UTF-8 字节，
    故任意文本的 token 数不超过其 UTF-8 字节数；上界因此取全部载荷字节之和。
    output_cap/thinking_cap 分别约束 completion（含推理，服务端 max_tokens 强制）
    与思考量（DashScope enable_thinking=False 记 0）；缺失即无法保证，发送前拒绝。
    """
    if output_cap is None or output_cap < 0:
        raise BudgetExceededError("调用无法保证输出上界，发送前拒绝")
    total = 0
    for text in texts:
        if text:
            total += len(text if isinstance(text, bytes) else str(text).encode("utf-8"))
    return total + int(output_cap) + int(thinking_cap)


def _total_tokens(usage) -> int | None:
    """按接口定义取总 token：优先 total_tokens，避免与分项重复叠加；缺 usage 视为未知。"""
    if not isinstance(usage, dict) or not usage:
        return None
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and total > 0:
        return int(total)
    parts = sum(v for k, v in usage.items() if isinstance(v, (int, float)) and k.endswith("tokens") and k != "total_tokens")
    return int(parts) if parts > 0 else None


class _ProcessLock:
    """跨进程单写者锁：fcntl(POSIX) / msvcrt(Windows)，进程退出由 OS 自动释放。"""

    def __init__(self, path: str):
        self._fd = open(path + ".lock", "a+b")
        try:
            if msvcrt is not None:
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
            elif fcntl is not None:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # 理论不可达：POSIX 有 fcntl、Windows 有 msvcrt
                raise BudgetLedgerError("当前平台无可用进程锁原语，无法保证单写者")
        except OSError as exc:
            self._fd.close()
            raise BudgetLedgerError("预算账本已被其他持有者占用（单写者约束），拒绝并发持有") from exc

    def release(self):
        try:
            if msvcrt is not None:
                self._fd.seek(0)
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        finally:
            self._fd.close()


class BudgetLedger:
    """单路径 JSONL 权威账本：预留/结算/halt/解除，重启可恢复，分模型记账。"""

    def __init__(self, path: str, limit: int, model_limits: dict[str, int]):
        if limit <= 0:
            raise BudgetLedgerError("预算总上限必须为正，拒绝在无上限时放行真实调用")
        if not model_limits or any(v <= 0 for v in model_limits.values()):
            raise BudgetLedgerError("必须配置分模型授权额度（budget.json p1 列），未授权模型一律拒绝")
        self.path = path
        self.limit = limit
        self.model_limits = dict(model_limits)
        self._lock = threading.RLock()
        self._process_lock = _ProcessLock(path)  # 第二持有者在此被拒绝
        self.actual = 0
        self.unknown = 0
        self.in_flight = 0
        self.per_model = {model: {"actual": 0, "unknown": 0, "in_flight": 0} for model in self.model_limits}
        self.halted = False
        self.halted_reason = ""
        self.corrupt = False
        self.corrupt_detail = ""
        self._pending: dict[str, tuple[str, int]] = {}
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
        model = record.get("model", "")
        bucket = self.per_model.setdefault(model, {"actual": 0, "unknown": 0, "in_flight": 0})
        if kind == "reserved":
            self.in_flight += int(record["estimate"])
            bucket["in_flight"] += int(record["estimate"])
        elif kind == "committed":
            estimate, actual = int(record["estimate"]), int(record["actual"])
            self.in_flight -= estimate
            bucket["in_flight"] -= estimate
            self.actual += actual
            bucket["actual"] += actual
            if actual > estimate:
                self.halted = True
                self.halted_reason = f"实报 {actual} 超预留 {estimate}（记录 {record.get('id')}）"
        elif kind == "committed_unknown":
            estimate = int(record["estimate"])
            self.in_flight -= estimate
            bucket["in_flight"] -= estimate
            self.unknown += estimate
            bucket["unknown"] += estimate
        elif kind == "clearance":
            self.halted = False
            self.halted_reason = ""
        else:
            raise ValueError(f"未知账本记录类型 {kind!r}")

    def _fail_corrupt(self, detail: str):
        self.corrupt = True
        self.corrupt_detail = detail
        self.actual = self.unknown = self.in_flight = 0
        self.per_model = {m: {"actual": 0, "unknown": 0, "in_flight": 0} for m in self.model_limits}
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
        """发送前预留；超限/未授权模型/损坏/写失败都在此拒绝，调用方不得触达供应商。"""
        with self._lock:
            if self.corrupt:
                raise BudgetLedgerError(f"账本损坏（{self.corrupt_detail}），fail closed，请人工核查账本")
            if self.halted:
                raise BudgetExceededError(f"预算已停止：{self.halted_reason}；需人工核查后写入 clearance 才能恢复")
            if model not in self.model_limits:
                raise BudgetExceededError(f"模型 {model!r} 不在本夜授权清单内，请求在到达供应商前被拒绝")
            used = self.actual + self.unknown + self.in_flight
            if used + estimate > self.limit:
                raise BudgetExceededError(
                    f"预算超限：实报 {self.actual} + 未知 {self.unknown} + 在途 {self.in_flight} + 本次预估 {estimate}"
                    f" = {used + estimate} 超过总授权 {self.limit}，请求在到达供应商前被拒绝"
                )
            model_used = self.per_model[model]
            model_used_total = model_used["actual"] + model_used["unknown"] + model_used["in_flight"]
            if model_used_total + estimate > self.model_limits[model]:
                raise BudgetExceededError(
                    f"模型 {model} 额度超限：实报 {model_used['actual']} + 未知 {model_used['unknown']}"
                    f" + 在途 {model_used['in_flight']} + 本次预估 {estimate} = {model_used_total + estimate}"
                    f" 超过授权 {self.model_limits[model]}，请求在到达供应商前被拒绝"
                )
            rid = uuid.uuid4().hex
            self._append({"type": "reserved", "id": rid, "at": self._stamp(), "pid": os.getpid(),
                          "kind": kind, "model": model, "estimate": estimate})
            self.in_flight += estimate
            self.per_model[model]["in_flight"] += estimate
            self._pending[rid] = (model, estimate)
            return rid

    def settle(self, rid: str, usage) -> None:
        """结算：usage 缺失/超时记 unknown（不释放、不记零）；实报超预留全额入账并 halt。

        结算写失败时账本无法记录本次实报，进程内立即 halt（fail closed）；
        预留行保持未结算，重启重放按 unknown 计入，不会少记。
        """
        with self._lock:
            pending = self._pending.pop(rid, None)
            if pending is None:
                raise BudgetLedgerError(f"结算找不到预留 {rid}，不静默覆盖")
            model, estimate = pending
            try:
                actual = _total_tokens(usage)
                if actual is None:
                    self._append({"type": "committed_unknown", "id": rid, "at": self._stamp(), "pid": os.getpid(),
                                  "model": model, "estimate": estimate})
                    self.in_flight -= estimate
                    self.per_model[model]["in_flight"] -= estimate
                    self.unknown += estimate
                    self.per_model[model]["unknown"] += estimate
                    return
                over = actual > estimate
                self._append({"type": "committed", "id": rid, "at": self._stamp(), "pid": os.getpid(),
                              "model": model, "estimate": estimate, "actual": actual, "over_reserve": over})
                self.in_flight -= estimate
                self.per_model[model]["in_flight"] -= estimate
                self.actual += actual
                self.per_model[model]["actual"] += actual
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
                    "limit": self.limit, "models": {m: dict(v) for m, v in self.per_model.items()},
                    "model_limits": dict(self.model_limits),
                    "halted": self.halted, "corrupt": self.corrupt}

    def close(self):
        """释放进程锁（测试模拟重启用；进程退出时 OS 也会自动释放）。"""
        self._process_lock.release()


_guards: dict[str, BudgetLedger] = {}
_guard_lock = threading.Lock()


def reset_budget_guard() -> None:
    """测试用：清空单写者缓存。"""
    with _guard_lock:
        _guards.clear()


def get_budget_guard():
    """按 settings 返回进程内单例账本；未配置路径/额度即 fail closed（真实入口拒绝启动）。"""
    from app.config import get_settings

    settings = get_settings()
    path = getattr(settings, "budget_ledger_path", "")
    if not path:
        return None
    with _guard_lock:
        guard = _guards.get(path)
        if guard is None:
            limit = int(getattr(settings, "budget_token_limit", 0) or 0)
            model_limits = getattr(settings, "budget_model_limits", None) or {}
            if limit <= 0:
                raise BudgetLedgerError("配置了预算账本路径但 budget_token_limit 非正，拒绝放行真实调用")
            guard = BudgetLedger(path, limit, {str(m): int(v) for m, v in model_limits.items()})
            _guards[path] = guard
        return guard

"""真实模型调用与计时。失败交给调用方，评测不得悄悄替换为 Mock。

所有通道在发送前经统一预算门控预留（超限/账本写失败在到达供应商前拒绝），
响应后按实际 usage 结算；缺 usage 记 unknown，不按零费用释放预留。
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from openai import OpenAI

from app.services.budget import BudgetExceededError, BudgetLedgerError, get_budget_guard
from app.services.run_service import check_cancelled, emit, live_model_stream, push_answer, push_live, tool_time_left

logger = logging.getLogger(__name__)

# 本文件所有 chat/tool_chat 通道的输出上界（max_tokens）；思考量在 DashScope 显式关闭记 0。
_CHAT_OUTPUT_CAP = 1536
_RERANK_OUTPUT_CAP = 64
_RERANK_WRAP_BYTES = 128  # rerank 请求协议包装余量
# 当前候选均支持关闭思考；遇到供应商特殊模型时再按探针结果补充。
_THINKING_DISABLE_UNSUPPORTED: set[str] = set()
_THINKING_CAP: dict[str, int] = {}
# 文本候选切换链（P1-Q-SWITCH）：由启动器按 budget.json generation_order 注入（JSON 列表，
# 不含首选模型）。仅本模型项目预留不足时按序切换；总额不足/认证/参数错误不切换。
_TEXT_CANDIDATES_ENV = "CHAT_MODEL_CANDIDATES"


def _text_candidates() -> list[str]:
    from app.config import get_settings

    settings = get_settings()
    raw = os.environ.get(_TEXT_CANDIDATES_ENV)
    try:
        parsed = json.loads(raw) if raw else settings.chat_model_candidates
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        expirations = settings.chat_model_expirations
        return sorted(
            dict.fromkeys(str(m) for m in parsed if expirations.get(str(m), "9999-12-31") >= today),
            key=lambda m: expirations.get(m, "9999-12-31"),
        )
    except ValueError:
        return []


def _available_chat_models(primary, excluded=()):
    from app.config import get_settings

    expirations = get_settings().chat_model_expirations
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    models = [
        m
        for m in dict.fromkeys([primary, *_text_candidates()])
        if m not in excluded and expirations.get(m, "9999-12-31") >= today
    ]
    if not models:
        raise BudgetExceededError("没有未到期且额度可用的聊天模型")
    return sorted(models, key=lambda m: expirations.get(m, "9999-12-31"))


def _is_per_model_budget_refusal(exc: Exception) -> bool:
    """区分拒绝类型：仅"本模型分模型预留不足"可切换候选；总额不足/halt 不可借换模型突破。"""
    msg = str(exc)
    return msg.startswith("模型") and "额度超限" in msg and "超过总授权" not in msg


def _is_total_budget_refusal(exc: Exception) -> bool:
    msg = str(exc)
    return "超过总授权" in msg or msg.startswith("预算已停止")


def _is_provider_transient(exc: Exception) -> bool:
    """供应商暂时性错误（429/5xx）：同模型最多额外重试一次（记账后）。"""
    status = getattr(exc, "status_code", None)
    return status in (429, 500, 502, 503, 504)


def _provider_error_code(exc: Exception):
    body = getattr(exc, "body", None)
    code = None
    if isinstance(body, dict):
        code = body.get("code")
        if not code and isinstance(body.get("error"), dict):
            code = body["error"].get("code")
    return code


def _is_provider_quota_exhausted(exc: Exception) -> bool:
    code = _provider_error_code(exc)
    return (
        code == "AllocationQuota.FreeTierOnly"
        or (code == "insufficient_quota" and getattr(exc, "status_code", None) in (402, 403, 429))
        or (getattr(exc, "status_code", None) == 403 and "AllocationQuota.FreeTierOnly" in str(exc))
    )


# chat 模板包装余量（已验证协议事实的保守放大约束，非实测常数硬编码）：
# Qwen 模板每条消息渲染 <|im_start|>role\n…<|im_end|>\n 等 ASCII 标记（单个标记 ≤11 字节），
# 每元素（消息/工具轮次/工具定义）放 128 字节，基础段（生成提示等）另放 128 字节；
# 配合"token ≤ UTF-8 字节数"即得随请求形状增长的可证明上界。
_BASE_WRAP_BYTES = 128
_WRAP_PER_ELEMENT_BYTES = 128


def estimate_chat_bound(messages: list[dict], tools: list[dict] | None, *, output_cap, thinking_cap: int = 0) -> int:
    """chat/tool_chat 的计费输入上界：完整实际请求（messages+tools）序列化字节 + 包装余量 + 封顶。"""
    if output_cap is None or output_cap < 0:
        raise BudgetExceededError("调用无法保证输出上界，发送前拒绝")
    request: dict = {"messages": messages}
    if tools:
        request["tools"] = tools
    payload = len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    elements = len(messages) + sum(len(m.get("tool_calls") or []) for m in messages) + len(tools or [])
    return payload + _BASE_WRAP_BYTES + _WRAP_PER_ELEMENT_BYTES * elements + int(output_cap) + int(thinking_cap)


def reserve_for_channel(calls: list, guard, kind: str, model: str, estimate: int):
    """发送前统一预留；拒绝时留 budget_refused 审计记录并抛出，调用方不触达供应商。"""
    if guard is None:
        raise BudgetLedgerError("真实模型通道缺少预算账本实例，fail closed")
    try:
        return guard, guard.reserve(kind, model, estimate)
    except (BudgetExceededError, BudgetLedgerError) as exc:
        calls.append(
            {
                "model": model,
                "kind": kind,
                "status": "budget_refused",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )
        raise


def settle_for_channel(record: dict, guard, reservation):
    """结算失败不得掩盖模型自身结果；账本内部已 fail closed，后续调用将被拒绝。"""
    if reservation is None:
        return
    try:
        guard.settle(reservation, record.get("usage") or None)
    except BudgetLedgerError as exc:
        record["settlement_error"] = type(exc).__name__
        logger.error("预算结算写入失败（%s），账本 fail closed，后续真实调用将被拒绝", exc)


class ChatAPI:
    def __init__(self, api_key: str, base_url: str, model: str):
        # 真实供应商入口：无持久账本配置即 fail closed，Mock 路径（MockLLM 等）不经过本类
        self._budget = get_budget_guard()
        if self._budget is None:
            raise BudgetLedgerError(
                "真实对话模型入口未配置持久预算账本（BUDGET_LEDGER_PATH/BUDGET_TOKEN_LIMIT），拒绝启动"
            )
        self.client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=60, max_retries=0)
        self.model = model
        self.is_dashscope = "aliyuncs.com" in (base_url or "")
        self.calls: list[dict] = []
        self._quota_exhausted = set()

    def _thinking_cap(self, model: str) -> int:
        # 可计费推理预留按模型配置（探针实测 2.4t-a95b 强制思考且 usage 返回 reasoning_tokens）
        return _THINKING_CAP.get(model, 0)

    def _reserve(
        self,
        kind: str,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        *,
        output_cap: int,
        thinking_cap: int = 0,
    ):
        estimate = estimate_chat_bound(
            messages, tools, output_cap=output_cap, thinking_cap=thinking_cap + self._thinking_cap(model)
        )
        return reserve_for_channel(self.calls, self._budget, kind, model, estimate)

    @staticmethod
    def _settle(guard, reservation, record: dict):
        settle_for_channel(record, guard, reservation)

    def chat(self, system: str, user: str, *, json_mode: bool = False, output_cap: int = _CHAT_OUTPUT_CAP) -> str:
        """文本生成，带最小候选切换（P1-Q-SWITCH）：
        - 首选模型"分模型预留不足"时按候选链切换（每次切换记入 calls 审计）；
        - 总额不足/halt、认证/参数错误不切换（不能借换模型突破总额）；
        - 供应商 429/5xx 同模型最多额外重试一次（每次尝试独立预留并记账）。"""
        check_cancelled()
        models = _available_chat_models(self.model, getattr(self, "_quota_exhausted", ()))
        i = 0
        while True:
            model = models[i]
            try:
                return self._chat_once(model, system, user, json_mode=json_mode, output_cap=output_cap)
            except BudgetExceededError as exc:
                if _is_per_model_budget_refusal(exc) and i + 1 < len(models):
                    self.calls.append(
                        {
                            "model": model,
                            "kind": "chat",
                            "status": "model_switch",
                            "to": models[i + 1],
                            "reason": str(exc)[:220],
                        }
                    )
                    logger.warning("模型 %s 分模型预留不足，按候选链切换至 %s", model, models[i + 1])
                    i += 1
                    continue
                raise
            except Exception as exc:
                if _is_provider_quota_exhausted(exc) and i + 1 < len(models):
                    self._quota_exhausted = {*getattr(self, "_quota_exhausted", ()), model}
                    self.calls.append(
                        {
                            "model": model,
                            "kind": "chat",
                            "status": "model_switch",
                            "to": models[i + 1],
                            "reason": "AllocationQuota.FreeTierOnly",
                        }
                    )
                    i += 1
                    continue
                if _is_provider_transient(exc):
                    self.calls.append(
                        {"model": model, "kind": "chat", "status": "transient_retry", "error": str(exc)[:220]}
                    )
                    logger.warning("模型 %s 供应商暂时性错误（%s），同模型额外重试一次", model, type(exc).__name__)
                    try:
                        return self._chat_once(model, system, user, json_mode=json_mode, output_cap=output_cap)
                    except Exception as exc2:
                        self.calls.append(
                            {
                                "model": model,
                                "kind": "chat",
                                "status": "transient_retry_failed",
                                "error": str(exc2)[:220],
                            }
                        )
                        raise
                raise

    def _chat_once(
        self, model: str, system: str, user: str, *, json_mode: bool = False, output_cap: int = _CHAT_OUTPUT_CAP
    ) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        guard, reservation = self._reserve("chat", model, messages, None, output_cap=output_cap)
        emit("model_input", {"kind": "chat", "model": model, "system": system, "user": user})
        started = time.perf_counter()
        record = {"model": model, "status": "error", "first_token_ms": None, "usage": {}}
        params = {}
        if self.is_dashscope and model not in _THINKING_DISABLE_UNSUPPORTED:
            params["extra_body"] = {"enable_thinking": False}
        if json_mode:
            params["response_format"] = {"type": "json_object"}
        text = []
        finish = None
        try:
            with self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0,
                max_tokens=output_cap,
                stream=True,
                stream_options={"include_usage": True},
                **params,
            ) as stream:
                for chunk in stream:
                    check_cancelled()
                    if chunk.usage:
                        record["usage"] = chunk.usage.model_dump()
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    content = choice.delta.content
                    if content:
                        if record["first_token_ms"] is None:
                            record["first_token_ms"] = (time.perf_counter() - started) * 1000
                        text.append(content)
                        if not json_mode:
                            push_answer(content)
                    finish = choice.finish_reason or finish
            if finish == "length" or not text:
                raise ValueError("模型输出被截断或为空")
            record.update(status="ok", finish_reason=finish)
            return "".join(text)
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            raise
        finally:
            record["elapsed_ms"] = (time.perf_counter() - started) * 1000
            self.calls.append(record)
            logger.info("model_api_usage kind=chat record=%s", json.dumps(record, ensure_ascii=False))
            emit("model_call", {"kind": "chat", **record})
            self._settle(guard, reservation, record)

    def tool_turn(self, messages: list[dict], tools: list[dict], *, allow_tools: bool = True) -> dict:
        check_cancelled()
        models = _available_chat_models(self.model, getattr(self, "_quota_exhausted", ()))
        for index, model in enumerate(models):
            try:
                return self._tool_turn_once(model, messages, tools, allow_tools=allow_tools)
            except Exception as exc:
                switch = _is_per_model_budget_refusal(exc) or _is_provider_quota_exhausted(exc)
                if not switch or index + 1 == len(models):
                    raise
                if _is_provider_quota_exhausted(exc):
                    self._quota_exhausted = {*getattr(self, "_quota_exhausted", ()), model}
                self.calls.append(
                    {
                        "model": model,
                        "kind": "tool_chat",
                        "status": "model_switch",
                        "to": models[index + 1],
                        "reason": type(exc).__name__,
                    }
                )

    def _tool_turn_once(self, model, messages, tools, *, allow_tools=True):
        guard, reservation = self._reserve("tool_chat", model, messages, tools, output_cap=_CHAT_OUTPUT_CAP)
        started = time.perf_counter()
        record = {"model": model, "status": "error", "usage": {}}
        try:
            if live_model_stream():
                result = self._stream_tool_turn(model, messages, tools, allow_tools, record, started)
                record["status"] = "ok"
                return result
            response = self.client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto" if allow_tools else "none",
                temperature=0,
                max_tokens=1536,
                **(
                    {"extra_body": {"enable_thinking": False}}
                    if self.is_dashscope and model not in _THINKING_DISABLE_UNSUPPORTED
                    else {}
                ),
            )
            record["usage"] = response.usage.model_dump() if response.usage else {}
            message = response.choices[0].message
            if response.choices[0].finish_reason == "length" or not (message.content or message.tool_calls):
                raise ValueError("模型工具调用输出为空或被截断")
            result = {"role": "assistant", "content": message.content or ""}
            if message.tool_calls:
                result["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.function.name, "arguments": c.function.arguments},
                    }
                    for c in message.tool_calls
                ]
            record["status"] = "ok"
            return result
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            code = _provider_error_code(exc)
            if isinstance(code, str):
                record["error_code"] = code[:120]
            raise
        finally:
            record["elapsed_ms"] = (time.perf_counter() - started) * 1000
            self.calls.append(record)
            logger.info("model_api_usage kind=tool_chat record=%s", json.dumps(record, ensure_ascii=False))
            emit("model_call", {"kind": "tool_chat", **record})
            self._settle(guard, reservation, record)

    def _stream_tool_turn(self, model, messages, tools, allow_tools, record, started):
        content, calls, finish = [], {}, None
        push_live({"type": "answer_reset"})
        with self.client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto" if allow_tools else "none",
            temperature=0,
            max_tokens=_CHAT_OUTPUT_CAP,
            stream=True,
            stream_options={"include_usage": True},
            **(
                {"extra_body": {"enable_thinking": False}}
                if self.is_dashscope and model not in _THINKING_DISABLE_UNSUPPORTED
                else {}
            ),
        ) as stream:
            for chunk in stream:
                check_cancelled()
                if chunk.usage:
                    record["usage"] = chunk.usage.model_dump()
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish = choice.finish_reason or finish
                delta = choice.delta
                if delta.content:
                    record.setdefault("first_token_ms", (time.perf_counter() - started) * 1000)
                    content.append(delta.content)
                    push_live({"type": "token", "delta": delta.content})
                for update in delta.tool_calls or []:
                    call = calls.setdefault(
                        update.index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    )
                    call["id"] += update.id or ""
                    if update.function:
                        call["function"]["name"] += update.function.name or ""
                        call["function"]["arguments"] += update.function.arguments or ""
        if finish == "length" or not (content or calls):
            raise ValueError("模型工具调用输出为空或被截断")
        result = {"role": "assistant", "content": "".join(content)}
        if calls:
            result["tool_calls"] = [call for _, call in sorted(calls.items())]
            push_live({"type": "answer_reset"})
        return result


class RerankAPI:
    def __init__(self, api_key: str, base_url: str, model: str):
        self._budget = get_budget_guard()
        if self._budget is None:
            raise BudgetLedgerError("真实重排入口未配置持久预算账本（BUDGET_LEDGER_PATH/BUDGET_TOKEN_LIMIT），拒绝启动")
        origin = urlsplit(base_url)
        self.url = f"{origin.scheme}://{origin.netloc}/api/v1/services/rerank/text-rerank/text-rerank"
        self.client = httpx.Client(headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
        self.model = model
        self.calls: list[dict] = []

    def rerank(self, query: str, chunks: list, top_k: int = 5) -> list:
        if not chunks:
            return []
        # 阿里云 rerank 官方成对计费口径：query tokens × 候选文档数 + 文档 tokens 总和。
        # top_n 只影响返回条数，不减少被送入并计费的候选数；按字节上界取保守值并含包装余量。
        query_bound = len(query.encode("utf-8"))
        estimate = query_bound * len(chunks) + sum(len(c.text.encode("utf-8")) for c in chunks)
        estimate = estimate + _RERANK_WRAP_BYTES + _RERANK_OUTPUT_CAP
        guard, reservation = reserve_for_channel(self.calls, self._budget, "rerank", self.model, estimate)
        started = time.perf_counter()
        record = {"model": self.model, "status": "error", "usage": {}}
        try:
            remaining = tool_time_left()
            response = self.client.post(
                self.url,
                **({"timeout": remaining} if remaining is not None else {}),
                json={
                    "model": self.model,
                    "input": {"query": query, "documents": [c.text for c in chunks]},
                    "parameters": {"top_n": min(top_k, len(chunks))},
                },
            )
            response.raise_for_status()
            body = response.json()
            rows = body["output"]["results"]
            if not rows:
                raise ValueError("重排服务没有返回候选")
            result = []
            for row in rows:
                chunk = chunks[row["index"]]
                chunk.score = float(row["relevance_score"])
                result.append(chunk)
            record.update(status="ok", usage=body.get("usage", {}))
            return result
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            raise
        finally:
            record["elapsed_ms"] = (time.perf_counter() - started) * 1000
            self.calls.append(record)
            logger.info("model_api_usage kind=rerank record=%s", json.dumps(record, ensure_ascii=False))
            emit("model_call", {"kind": "rerank", **record})
            settle_for_channel(record, guard, reservation)

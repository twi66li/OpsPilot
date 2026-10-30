"""真实模型调用与计时。失败交给调用方，评测不得悄悄替换为 Mock。"""
import json
import logging
import time
from urllib.parse import urlsplit

import httpx
from openai import OpenAI

from app.services.run_service import emit, tool_time_left

logger = logging.getLogger(__name__)


class ChatAPI:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=60, max_retries=0)
        self.model = model
        self.is_dashscope = "aliyuncs.com" in (base_url or "")
        self.calls: list[dict] = []

    def chat(self, system: str, user: str, *, json_mode: bool = False) -> str:
        emit("model_input", {"kind": "chat", "model": self.model, "system": system, "user": user})
        started = time.perf_counter()
        record = {"model": self.model, "status": "error", "first_token_ms": None, "usage": {}}
        params = {}
        if self.is_dashscope:
            params["extra_body"] = {"enable_thinking": False}
        if json_mode:
            params["response_format"] = {"type": "json_object"}
        text = []
        finish = None
        try:
            with self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0,
                max_tokens=1536,
                stream=True,
                stream_options={"include_usage": True},
                **params,
            ) as stream:
                for chunk in stream:
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

    def tool_turn(self, messages: list[dict], tools: list[dict], *, allow_tools: bool = True) -> dict:
        started = time.perf_counter()
        record = {"model": self.model, "status": "error", "usage": {}}
        try:
            response = self.client.chat.completions.create(
                model=self.model, messages=messages, tools=tools,
                tool_choice="auto" if allow_tools else "none", temperature=0, max_tokens=1536,
                **({"extra_body": {"enable_thinking": False}} if self.is_dashscope else {}),
            )
            record["usage"] = response.usage.model_dump() if response.usage else {}
            message = response.choices[0].message
            if response.choices[0].finish_reason == "length" or not (message.content or message.tool_calls):
                raise ValueError("模型工具调用输出为空或被截断")
            result = {"role": "assistant", "content": message.content or ""}
            if message.tool_calls:
                result["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                    for c in message.tool_calls
                ]
            record["status"] = "ok"
            return result
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            raise
        finally:
            record["elapsed_ms"] = (time.perf_counter() - started) * 1000
            self.calls.append(record)
            logger.info("model_api_usage kind=tool_chat record=%s", json.dumps(record, ensure_ascii=False))
            emit("model_call", {"kind": "tool_chat", **record})


class RerankAPI:
    def __init__(self, api_key: str, base_url: str, model: str):
        origin = urlsplit(base_url)
        self.url = f"{origin.scheme}://{origin.netloc}/api/v1/services/rerank/text-rerank/text-rerank"
        self.client = httpx.Client(headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
        self.model = model
        self.calls: list[dict] = []

    def rerank(self, query: str, chunks: list, top_k: int = 5) -> list:
        if not chunks:
            return []
        started = time.perf_counter()
        record = {"model": self.model, "status": "error", "usage": {}}
        try:
            remaining = tool_time_left()
            response = self.client.post(self.url, **({"timeout": remaining} if remaining is not None else {}), json={
                "model": self.model,
                "input": {"query": query, "documents": [c.text for c in chunks]},
                "parameters": {"top_n": min(top_k, len(chunks))},
            })
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

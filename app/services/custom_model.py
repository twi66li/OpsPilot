"""用户自带模型（BYOK）单次运行内存注入与 SSRF 校验；凭据绝不落库、不进日志。"""

from __future__ import annotations

import ipaddress
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.services.http_probe import _resolve_host

HEADER_ENDPOINT = "X-OpsPilot-Model-Endpoint"
HEADER_MODEL = "X-OpsPilot-Model-Id"
HEADER_KEY = "X-OpsPilot-Model-Key"
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_RUN_TTL_SECONDS = 900.0

_active_custom_llm: ContextVar[Any | None] = ContextVar("opspilot_custom_llm", default=None)
_ephemeral_lock = threading.Lock()
_ephemeral_runs: dict[str, tuple[float, CustomModelConfig]] = {}


@dataclass(frozen=True)
class CustomModelConfig:
    endpoint: str
    model: str
    api_key: str = field(default="", repr=False)


def _is_loopback_host(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_custom_model_endpoint(endpoint: str, *, allow_loopback: bool = False) -> str:
    raw = (endpoint or "").strip()
    parsed = urlsplit(raw)
    if (
        not raw
        or parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("请输入不含账号密码、查询参数或锚点的模型网关地址")

    host = parsed.hostname.lower().strip("[]")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("模型网关端口无效") from exc
    if not (1 <= port <= 65535):
        raise ValueError("模型网关端口无效")

    is_loopback = _is_loopback_host(host)
    if parsed.scheme == "http":
        if not (allow_loopback and is_loopback):
            raise ValueError("模型网关必须使用 HTTPS 地址")
    elif is_loopback and not allow_loopback:
        raise ValueError("生产环境禁止将模型网关指向本机回环地址")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if not is_loopback and (not literal.is_global or literal.is_multicast):
            raise ValueError("禁止将模型网关指向内网、链路本地或保留地址")
    elif not is_loopback:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("模型网关域名格式无效") from exc
        if not allow_loopback:
            try:
                addresses = _resolve_host(ascii_host, port)
            except ValueError:
                raise
            except OSError as exc:
                raise ValueError("模型网关域名无法解析，请检查 Base URL") from exc
            if not addresses or any(
                not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast
                for ip in addresses
            ):
                raise ValueError("模型网关域名解析到非公网地址，已拒绝连接")

    return raw.rstrip("/")


def parse_custom_model_headers(request: Request, *, allow_loopback: bool) -> CustomModelConfig | None:
    endpoint = (request.headers.get(HEADER_ENDPOINT) or "").strip()
    model = (request.headers.get(HEADER_MODEL) or "").strip()
    api_key = (request.headers.get(HEADER_KEY) or "").strip()
    if not endpoint and not model and not api_key:
        return None
    if not endpoint or not model:
        raise HTTPException(400, "自带模型配置不完整，请填写 Base URL 与模型 ID")
    if len(endpoint) > 512 or len(model) > 128 or len(api_key) > 512:
        raise HTTPException(400, "自带模型配置字段超出长度限制")
    try:
        normalized = validate_custom_model_endpoint(endpoint, allow_loopback=allow_loopback)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    host = (urlsplit(normalized).hostname or "").lower().strip("[]")
    if not api_key and not (allow_loopback and _is_loopback_host(host)):
        raise HTTPException(400, "非本机模型网关需要填写 API Key")
    return CustomModelConfig(endpoint=normalized, model=model, api_key=api_key)


def build_custom_llm(config: CustomModelConfig):
    from app.agent.llm import OpenAIChatLLM

    return OpenAIChatLLM(
        api_key=config.api_key or "sk-local-custom-model",
        base_url=config.endpoint,
        model=config.model,
        strict=True,
        skip_budget=True,
        allow_model_switch=False,
        follow_redirects=False,
    )


@contextmanager
def custom_llm_context(config: CustomModelConfig | None):
    if config is None:
        yield
        return
    token = _active_custom_llm.set(build_custom_llm(config))
    try:
        yield
    finally:
        _active_custom_llm.reset(token)


def active_llm(container):
    return _active_custom_llm.get() or container.llm


def has_custom_llm() -> bool:
    return _active_custom_llm.get() is not None


def _prune_expired(now: float) -> None:
    expired = [run_id for run_id, (expires_at, _) in _ephemeral_runs.items() if expires_at <= now]
    for run_id in expired:
        _ephemeral_runs.pop(run_id, None)


def bind_run_custom_model(run_id: str, config: CustomModelConfig | None) -> None:
    if config is None:
        return
    now = time.monotonic()
    with _ephemeral_lock:
        _prune_expired(now)
        _ephemeral_runs[run_id] = (now + _RUN_TTL_SECONDS, config)


def pop_run_custom_model(run_id: str) -> CustomModelConfig | None:
    now = time.monotonic()
    with _ephemeral_lock:
        _prune_expired(now)
        entry = _ephemeral_runs.pop(run_id, None)
    return entry[1] if entry is not None else None


def clear_run_custom_model(run_id: str) -> None:
    with _ephemeral_lock:
        _ephemeral_runs.pop(run_id, None)

"""主动单次HTTP探测：固定已校验IP，保留原Host与TLS校验，禁止跳转。"""

import ipaddress
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx


def probe_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("请输入不含账号密码的HTTP或HTTPS网址")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("网址端口无效") from exc
    if port not in (80, 443):
        raise ValueError("当前只支持80和443端口")
    host = parsed.hostname.encode("idna").decode("ascii")
    addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    if not addresses or any(
        not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in addresses
    ):
        raise ValueError("只能检查公开网络地址，禁止访问内网、回环和云元数据地址")
    ip = sorted(addresses)[0]
    target = httpx.URL(url).copy_with(host=ip)
    source = str(httpx.URL(url).copy_with(query=None))
    started = time.perf_counter()
    result = {
        "source_url": source,
        "source_kind": "read_only_http",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "scope": "单次HTTP GET响应，仅反映所选地址；未读取网站日志，也不能证明网站整体恢复",
    }
    try:
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            with client.stream(
                "GET",
                target,
                headers={"Host": host, "User-Agent": "OpsPilot-HealthCheck/1"},
                extensions={"sni_hostname": host},
            ) as response:
                result.update(
                    http_status=response.status_code,
                    state="normal"
                    if 200 <= response.status_code < 300
                    else "unknown"
                    if 300 <= response.status_code < 400
                    else "abnormal",
                )
                if response.is_redirect:
                    result["note"] = "地址返回跳转，平台未继续访问跳转目标"
    except httpx.HTTPError as exc:
        result.update(
            state="unknown", error_type=type(exc).__name__, note="本次请求未完成，请检查网络和TLS；不能据此确认网站故障"
        )
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result

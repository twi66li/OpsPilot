"""固定演练动作：关闭测试服务的注入故障，使用实际请求验证结果。"""
import time

import httpx

ACTION = "reset_demo_fault"
ACTION_LABEL = "关闭 order-service 演练故障注入"


def _probe(client):
    samples = []
    for _ in range(3):
        start = time.perf_counter()
        response = client.get("/work")
        samples.append({"status_code": response.status_code, "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                        "request_id": response.headers.get("X-Request-ID", "")})
    return {"samples": samples, "healthy": all(s["status_code"] == 200 and s["elapsed_ms"] < 1000 for s in samples)}


def execute_demo_reset(base_url, result):
    # 目标地址只来自服务端配置；没有模型可填写的URL、命令或脚本。
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=5.0, trust_env=False) as client:
        before = client.get("/control")
        before.raise_for_status()
        state = before.json()
        if state.get("environment") != "test" or state.get("service") != "order-service":
            raise ValueError("目标不是指定的演练服务")
        result["before"] = {"control": state, **_probe(client)}
        response = client.post("/control", json={"delay_ms": 0, "error_every": 0, "error_code": "ERR_INJECTED"})
        response.raise_for_status()
        result["action_response"] = response.json()
        after = client.get("/control")
        after.raise_for_status()
        result["after"] = {"control": after.json(), **_probe(client)}
        config = result["after"]["control"]["fault_injection"]
        return result["after"]["healthy"] and config["delay_ms"] == 0 and config["error_every"] == 0

import time
import urllib.error
import urllib.request

for attempt in range(45):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=3) as response:
            if response.status == 200:
                break
    except (urllib.error.URLError, OSError):
        time.sleep(1)
else:
    raise RuntimeError("API依赖未在预期时间内就绪")

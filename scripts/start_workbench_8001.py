"""早期 8001 评测工作台随对应预算批次归档。"""

if __name__ == "__main__":
    raise SystemExit(
        "8001 历史评测入口已归档。本地工作台请运行 "
        "docker compose --profile app --profile observability up -d，并访问 http://127.0.0.1:8000。"
    )

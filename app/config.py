"""全局配置：全部通过环境变量 / .env 覆盖，默认值支持零依赖离线运行。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "aiops-diagnosis-agent"

    # ---- 存储 ----
    database_url: str = "sqlite:///./data/app.db"
    kb_storage_path: str = "./data/knowledge"

    # ---- 认证 ----
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 720
    admin_username: str = "admin"
    admin_password: str = "admin123"

    # ---- Qdrant 向量库 ----
    qdrant_mode: str = "memory"  # memory | local | remote
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "kb_chunks"

    # ---- Embedding / LLM ----
    embedding_provider: str = "mock"  # mock | openai
    embedding_dim: int = 256
    llm_provider: str = "mock"  # mock | openai
    model_strict: bool = False  # 真实评测失败直接报错，不回退 Mock
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    chat_model: str = "qwen3.8-max"
    chat_model_candidates: list[str] = []
    chat_model_expirations: dict[str, str] = {}
    embedding_model: str = "text-embedding-3-small"

    # ---- 检索 ----
    retrieval_mode: str = "hybrid"  # dense | bm25 | hybrid
    dense_top_k: int = 5
    bm25_top_k: int = 5
    top_k: int = 5
    rrf_k: int = 60
    # 送入模型与裁判的证据摘录上界（字符）。截断在行边界处收口，绝不把一行日志从中间切断；
    # 评测记录与裁判看到的就是生成器实际收到的摘录。历史批次（night20261031）为硬 [:500] 截断。
    evidence_excerpt_chars: int = 500
    # 行范围完整摘录的防御上限（字符）：BGL 35 行窗口 ≈5,300 字符、最大 61 行窗口 ≈9,200 字符，
    # 完整计数类问题必须整窗可见；超限整行截断并在 range_assembly 报告 targets_dropped_by_budget。
    evidence_range_chars: int = 12000
    rerank_enabled: bool = False
    rerank_provider: str = "heuristic"  # heuristic | cross_encoder | dashscope（T7：DashScope 原生 text-rerank）
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"
    rate_limit_per_minute: int = 60

    # ---- 测试服务监控 ----
    metrics_provider: str = "mock"  # 离线测试显式 Mock；Compose 使用 prometheus
    prometheus_url: str = "http://localhost:19090"
    prometheus_timeout_seconds: float = 3.0

    checkpoint_path: str = ""  # 离线测试为空；单进程 Compose 使用持久卷中的 SQLite 文件
    agent_mode: str = "rule"  # rule | native；离线测试保留规则链
    tool_timeout_seconds: float = 8.0
    max_tool_calls: int = 5
    logs_url: str = "http://localhost:18081"
    remediation_enabled: bool = False
    mcp_enabled: bool = False
    retrieval_subagent_enabled: bool = False
    retrieval_subagent_timeout_seconds: float = 120.0

    # ---- Langfuse ----
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # ---- 预算门控（真实模型调用统一入口；路径留空=不启用，仅离线 Mock 使用）----
    budget_ledger_path: str = ""
    budget_token_limit: int = 0
    budget_model_limits: dict[str, int] = {}  # 按 budget.json p1 列的分模型授权；未授权模型一律拒绝


@lru_cache
def get_settings() -> Settings:
    return Settings()

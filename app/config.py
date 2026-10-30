"""全局配置：全部通过环境变量 / .env 覆盖，默认值支持零依赖离线运行。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "aiops-diagnosis-agent"

    # ---- 存储 ----
    database_url: str = "sqlite:///./data/app.db"

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
    embedding_model: str = "text-embedding-3-small"

    # ---- 检索 ----
    retrieval_mode: str = "hybrid"  # dense | bm25 | hybrid
    dense_top_k: int = 5
    bm25_top_k: int = 5
    top_k: int = 5
    rrf_k: int = 60
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

    # ---- Langfuse ----
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # ---- 预算门控（真实模型调用统一入口；路径留空=不启用，仅离线 Mock 使用）----
    budget_ledger_path: str = ""
    budget_token_limit: int = 0


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""全局配置：全部通过环境变量 / .env 覆盖，默认值支持零依赖离线运行。"""

from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class DbConnectionBudget:
    """PostgreSQL 连接池配额核算结果（按多副本上限 + 管理预留合并核定）。"""

    pg_max_connections: int
    replicas: int
    admin_reserved: int
    app_pool_size: int
    app_max_overflow: int
    app_pool_max: int
    checkpoint_pool_min: int
    checkpoint_pool_max: int
    per_replica_max: int
    total_app_max: int
    total_budget: int
    headroom: int
    fits: bool

    def to_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def calculate_db_connection_budget(settings: "Settings | None" = None) -> DbConnectionBudget:
    """合并核算业务连接池 + LangGraph 检查点连接池 + 管理预留连接。"""
    cfg = settings or get_settings()
    pg_max = int(cfg.db_max_connections)
    replicas = int(cfg.db_replica_budget)
    admin_reserved = int(cfg.db_admin_reserved_connections)
    app_pool_size = int(cfg.db_pool_size)
    app_max_overflow = int(cfg.db_max_overflow)
    checkpoint_min = int(cfg.checkpoint_pool_min_size)
    checkpoint_max = int(cfg.checkpoint_pool_max_size)

    if pg_max < 1:
        raise ValueError("DB_MAX_CONNECTIONS 必须 >= 1")
    if replicas < 1:
        raise ValueError("DB_REPLICA_BUDGET 必须 >= 1")
    if admin_reserved < 1:
        raise ValueError("DB_ADMIN_RESERVED_CONNECTIONS 必须 >= 1")
    if app_pool_size < 1 or app_max_overflow < 0:
        raise ValueError("DB_POOL_SIZE 必须 >= 1 且 DB_MAX_OVERFLOW 必须 >= 0")
    if checkpoint_min < 1 or checkpoint_max < checkpoint_min:
        raise ValueError("CHECKPOINT_POOL_MIN_SIZE 必须 >= 1 且 CHECKPOINT_POOL_MAX_SIZE >= MIN_SIZE")
    if int(cfg.db_pool_timeout) < 1:
        raise ValueError("DB_POOL_TIMEOUT 必须 >= 1")

    app_pool_max = app_pool_size + app_max_overflow
    per_replica_max = app_pool_max + checkpoint_max
    total_app_max = per_replica_max * replicas
    total_budget = total_app_max + admin_reserved
    headroom = pg_max - total_app_max
    return DbConnectionBudget(
        pg_max_connections=pg_max,
        replicas=replicas,
        admin_reserved=admin_reserved,
        app_pool_size=app_pool_size,
        app_max_overflow=app_max_overflow,
        app_pool_max=app_pool_max,
        checkpoint_pool_min=checkpoint_min,
        checkpoint_pool_max=checkpoint_max,
        per_replica_max=per_replica_max,
        total_app_max=total_app_max,
        total_budget=total_budget,
        headroom=headroom,
        fits=total_budget <= pg_max,
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    app_name: str = "aiops-diagnosis-agent"
    app_env: Literal["development", "demo", "production"] = "development"
    registration_enabled: bool = False
    visitor_enabled: bool = False
    cors_origins: list[str] = []
    auth_rate_limit_per_minute: int = 30

    # ---- 存储 ----
    database_url: str = "sqlite:///./data/app.db"
    # PostgreSQL 连接池配额（默认按 max_connections=30、双副本核算：单副本 5+3+4=12，双副本 24，管理预留 6）
    db_max_connections: int = 30
    db_replica_budget: int = 2
    db_admin_reserved_connections: int = 6
    db_pool_size: int = 5
    db_max_overflow: int = 3
    db_pool_timeout: int = 30
    checkpoint_pool_min_size: int = 1
    checkpoint_pool_max_size: int = 4
    kb_storage_path: str = "./data/knowledge"

    # ---- 认证 ----
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    admin_username: str = "admin"
    admin_password: str = "admin123"

    # ---- 向量库 ----
    vector_store_backend: Literal["milvus", "memory"] = "milvus"
    milvus_uri: str = "http://127.0.0.1:19531"
    milvus_token: str = ""
    milvus_db_name: str = "default"
    milvus_collection: str = "opspilot_chunks"

    # 旧向量迁移的源配置
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
    rerank_provider: str = "heuristic"  # heuristic | cross_encoder | dashscope
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"
    rate_limit_per_minute: int = 60
    retrieval_cache_ttl: int = 120

    # ---- 测试服务监控 ----
    metrics_provider: str = "mock"  # 离线测试显式 Mock；Compose 使用 prometheus
    prometheus_url: str = "http://localhost:19090"
    prometheus_timeout_seconds: float = 3.0

    checkpoint_path: str = ""  # 离线测试为空；单进程 Compose 使用持久卷中的 SQLite 文件
    agent_mode: str = "rule"  # rule | native；离线测试保留规则链
    tool_timeout_seconds: float = 8.0
    max_tool_calls: int = 5
    max_concurrent_runs: int = 1
    logs_url: str = "http://localhost:18081"
    drill_enabled: bool = False
    drill_service: str = Field(default="order-service", max_length=64, pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*-service$")
    remediation_enabled: bool = False
    strict_four_eyes: bool = False  # 生产/企业可开启：禁止同一账号自提自批工单或演练修复
    mcp_enabled: bool = False
    retrieval_subagent_enabled: bool = False
    retrieval_subagent_timeout_seconds: float = 120.0

    # ---- Langfuse ----
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # ---- 预算门控（真实模型调用统一入口；路径留空=不启用，仅离线 Mock 使用）----
    budget_ledger_backend: Literal["auto", "jsonl", "db"] = "auto"  # auto: PG 走 db，其余按 path 走 jsonl
    budget_ledger_scope: str = "default"
    budget_ledger_path: str = ""
    budget_token_limit: int = 0
    budget_model_limits: dict[str, int] = {}  # 按 budget.json p1 列的分模型授权；未授权模型一律拒绝

    @property
    def drill_remediation_enabled(self) -> bool:
        return self.drill_enabled and self.remediation_enabled

    @property
    def drill_mcp_enabled(self) -> bool:
        return self.drill_enabled and self.mcp_enabled

    @model_validator(mode="after")
    def validate_connection_budget(self):
        budget = calculate_db_connection_budget(self)
        if not budget.fits:
            raise ValueError(
                f"PostgreSQL 连接池总配额超限：单副本上限 {budget.per_replica_max} "
                f"(业务 {budget.app_pool_size}+{budget.app_max_overflow} + 检查点 {budget.checkpoint_pool_max}) "
                f"× {budget.replicas} 副本 + 管理预留 {budget.admin_reserved} = {budget.total_budget} "
                f"> max_connections={budget.pg_max_connections}"
            )
        return self

    @model_validator(mode="after")
    def production_security(self):
        if self.app_env == "production":
            if self.jwt_secret == "dev-secret-change-me" or len(self.jwt_secret) < 32:
                raise ValueError("生产环境 JWT_SECRET 必须使用至少32字符的独立随机密钥")
            if self.admin_password == "admin123" or len(self.admin_password) < 12:
                raise ValueError("生产环境 ADMIN_PASSWORD 必须配置至少12字符的独立密码")
            if "*" in self.cors_origins:
                raise ValueError("生产环境 CORS_ORIGINS 必须指定明确来源")
            if self.database_url.startswith("sqlite"):
                raise ValueError("生产环境 DATABASE_URL 必须使用持久化数据库")
            if self.vector_store_backend != "milvus" or not self.milvus_uri.startswith(("http://", "https://")):
                raise ValueError("生产环境需要 Milvus 服务，请配置 VECTOR_STORE_BACKEND=milvus 和 MILVUS_URI")
            if self.llm_provider == "mock" or self.embedding_provider == "mock":
                raise ValueError("生产环境 LLM_PROVIDER 和 EMBEDDING_PROVIDER 不能使用 mock")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

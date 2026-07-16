import os
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用全局配置，支持环境变量 + .env 文件"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 服务
    APP_NAME: str = "Knowledge Base Management"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    RUNNING_IN_DOCKER: bool = False

    # ==================== 数据存储路径 ====================
    KBDATA_DIR: str = ""

    # 认证配置
    API_KEY_FILE: str = "/app/config/api_keys.json"
    ADMIN_ACCOUNTS_FILE: str = "/app/config/admin_accounts.json"
    ADMIN_INITIAL_USERNAME: str = "admin"
    ADMIN_INITIAL_PASSWORD: str = "123456"
    SESSION_SECRET: str = ""
    SESSION_MAX_AGE: int = 86400

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    SEARCH_CACHE_TTL: int = 300
    SEARCH_TOTAL_TIMEOUT_MS: int = 10000
    SEARCH_ENRICH_TIMEOUT_MS: int = 1500
    SEARCH_VECTOR_TIMEOUT_MS: int = 7000
    SEARCH_KEYWORD_TIMEOUT_MS: int = 3000
    SEARCH_STRUCTURE_TIMEOUT_MS: int = 2000
    SEARCH_NEIGHBOR_TIMEOUT_MS: int = 1000
    SEARCH_MAX_CONCURRENCY: int = 12
    SEARCH_QUEUE_TIMEOUT_MS: int = 2000
    SEARCH_CONTEXT_MAX_CHARS: int = 2000

    # 图谱关联检索（图谱不存在时自动降级为普通混合检索）
    GRAPH_RETRIEVAL_ENABLED: bool = True
    GRAPH_RETRIEVAL_TIMEOUT_MS: int = 800
    GRAPH_RETRIEVAL_WEIGHT: float = 0.35
    GRAPH_RETRIEVAL_MAX_RESULTS: int = 3
    GRAPH_RETRIEVAL_MAX_HOPS: int = 2
    GRAPH_RETRIEVAL_SEED_COUNT: int = 3
    GRAPH_RETRIEVAL_MIN_EDGE_WEIGHT: float = 0.25

    # Chroma
    CHROMA_HOST: str = "localhost"
    CHROMA_PORT: int = 8001
    CHROMA_COLLECTION: str = "knowledge_base_management"

    # Ollama
    OLLAMA_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "bge-m3"
    EMBEDDING_FALLBACKS: str = ""
    EMBEDDING_HEALTH_CACHE_TTL: int = 30
    EMBEDDING_FAILURE_THRESHOLD: int = 3
    EMBEDDING_CIRCUIT_COOLDOWN: int = 30
    EMBEDDING_REQUEST_TIMEOUT_SECONDS: float = 120.0
    EMBEDDING_HEALTH_TIMEOUT_SECONDS: float = 5.0
    EMBEDDING_MAX_CONNECTIONS: int = 24
    EMBEDDING_MAX_KEEPALIVE_CONNECTIONS: int = 8
    EMBEDDING_MAX_BATCH_SIZE: int = 200

    # MinIO
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "kb-sources"
    MINIO_SECURE: bool = False

    # 切片
    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 50

    # 锁
    WRITE_LOCK_KEY: str = "kb:write_lock"
    WRITE_LOCK_TTL: int = 30

    # 限流
    RATE_LIMIT_DEFAULT: int = 30

    # CORS
    EXTERNAL_DOMAIN: str = "kb.company.com"
    INTERNAL_DOMAIN: str = "kb.internal.company.com"
    CORS_ORIGINS: str = "*"

    _DEFAULT_ADMIN_FILE = "/app/config/admin_accounts.json"
    _DEFAULT_API_KEY_FILE = "/app/config/api_keys.json"

    @model_validator(mode="after")
    def _derive_config_paths(self):
        """当 KBDATA_DIR 设置时，自动派生配置文件路径"""
        if not self.KBDATA_DIR:
            return self
        data_dir = self.KBDATA_DIR.rstrip("/\\")
        if data_dir and not os.path.isabs(data_dir):
            project_root = Path(__file__).resolve().parents[2]
            data_dir = str((project_root / data_dir).resolve())
            object.__setattr__(self, "KBDATA_DIR", data_dir)
        if self.ADMIN_ACCOUNTS_FILE == self._DEFAULT_ADMIN_FILE:
            object.__setattr__(self, "ADMIN_ACCOUNTS_FILE",
                               os.path.join(data_dir, "config", "admin_accounts.json"))
        if self.API_KEY_FILE == self._DEFAULT_API_KEY_FILE:
            object.__setattr__(self, "API_KEY_FILE",
                               os.path.join(data_dir, "config", "api_keys.json"))
        return self

    @model_validator(mode="after")
    def _validate_security_settings(self):
        """安全设置校验 — 在非 DEBUG 模式下拒绝弱配置"""
        if not self.DEBUG:
            if not self.SESSION_SECRET or len(self.SESSION_SECRET) < 32:
                raise ValueError(
                    "SESSION_SECRET must be at least 32 characters in production. "
                    "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

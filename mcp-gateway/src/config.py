import os
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用全局配置，支持环境变量 + .env 文件"""

    # 服务
    APP_NAME: str = "Knowledge Base Management"
    DEBUG: bool = False

    # ==================== 数据存储路径 ====================
    KBDATA_DIR: str = ""

    # 认证配置
    API_KEY_FILE: str = "/app/config/api_keys.json"
    ADMIN_ACCOUNTS_FILE: str = "/app/config/admin_accounts.json"
    SESSION_SECRET: str = "change-me-in-production"
    SESSION_MAX_AGE: int = 86400

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Chroma
    CHROMA_HOST: str = "localhost"
    CHROMA_PORT: int = 8001
    CHROMA_COLLECTION: str = "knowledge_base_management"

    # Ollama
    OLLAMA_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "bge-m3"

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

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    _DEFAULT_ADMIN_FILE = "/app/config/admin_accounts.json"
    _DEFAULT_API_KEY_FILE = "/app/config/api_keys.json"

    @model_validator(mode="after")
    def _derive_config_paths(self):
        """当 KBDATA_DIR 设置时，自动派生配置文件路径"""
        if not self.KBDATA_DIR:
            return self
        data_dir = self.KBDATA_DIR.rstrip("/\\")
        if self.ADMIN_ACCOUNTS_FILE == self._DEFAULT_ADMIN_FILE:
            object.__setattr__(self, "ADMIN_ACCOUNTS_FILE",
                               os.path.join(data_dir, "config", "admin_accounts.json"))
        if self.API_KEY_FILE == self._DEFAULT_API_KEY_FILE:
            object.__setattr__(self, "API_KEY_FILE",
                               os.path.join(data_dir, "config", "api_keys.json"))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # 服务
    APP_NAME: str = "Knowledge Base Management"
    DEBUG: bool = False

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

    # 认证
    API_KEY_FILE: str = "/app/config/api_keys.json"
    ADMIN_ACCOUNTS_FILE: str = "/app/config/admin_accounts.json"
    SESSION_SECRET: str = "change-me-in-production"
    SESSION_MAX_AGE: int = 86400  # 24小时

    # 切片
    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 50

    # 锁
    WRITE_LOCK_KEY: str = "kb:write_lock"
    WRITE_LOCK_TTL: int = 30

    # 限流
    RATE_LIMIT_DEFAULT: int = 30  # 每分钟请求数

    # CORS
    CORS_ORIGINS: str = "*"  # 逗号分隔，如 "https://app.company.com,https://admin.company.com"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()

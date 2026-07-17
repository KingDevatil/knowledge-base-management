from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime


class APIKeyInfo(BaseModel):
    key_prefix: str
    applicant: str
    applicant_note: str = ""
    role: str = "user"
    scope: list[str] = Field(default_factory=lambda: ["read"])
    path_mode: Literal["all", "restricted"] = "all"
    allowed_paths: list[str] = Field(default_factory=list)
    rate_limit: int = 30
    status: Literal["active", "expired", "revoked"] = "active"
    duration: str = "7d"
    created_at: str
    expires_at: str
    revoked_at: str | None = None
    revoked_by: str | None = None
    created_by: str = "admin"
    last_used_at: str | None = None
    use_count: int = 0


class DocumentInfo(BaseModel):
    doc_id: str
    title: str
    path: str = ""
    tags: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    chunk_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class SearchResult(BaseModel):
    content: str
    title: str
    path: str = ""
    source_path: str = ""
    entities: list[str] = Field(default_factory=list)
    doc_id: str = ""
    chunk_index: int = 0
    total_chunks: int = 0
    score: float = 0.0


class AdminAccount(BaseModel):
    username: str
    password_hash: str
    role: Literal["super_admin", "admin"] = "admin"
    created_at: str


# ---- API Request Models (input validation) ----

MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB


class AddDocumentRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500, description="文档标题")
    content: str = Field(..., min_length=1, max_length=MAX_CONTENT_LENGTH, description="文档 Markdown 内容")
    path: str = Field(default="", max_length=500, description="目录路径")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    created_by: str = Field(default="api", max_length=100)


class UpdateDocumentRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1, max_length=MAX_CONTENT_LENGTH)
    path: str = Field(default="", max_length=500)
    tags: list[str] = Field(default_factory=list)
    updated_by: str = Field(default="api", max_length=100)


class SimilarDocumentsRequest(BaseModel):
    title: str = Field(default="", max_length=500)
    content: str = Field(..., min_length=1, max_length=MAX_CONTENT_LENGTH)
    path: str = Field(default="", max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)


class UpsertDocumentRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1, max_length=MAX_CONTENT_LENGTH)
    path: str = Field(default="", max_length=500)
    tags: list[str] = Field(default_factory=list)
    match_strategy: Literal["title_path", "hash", "semantic"] = "title_path"
    on_conflict: Literal["update", "skip", "create_new"] = "update"
    created_by: str = Field(default="api", max_length=100)


class ReindexByPathRequest(BaseModel):
    path: str = Field(default="", max_length=500, description="要重索引的目录路径")

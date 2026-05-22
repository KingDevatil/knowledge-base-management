from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime


class APIKeyInfo(BaseModel):
    key_prefix: str
    applicant: str
    applicant_note: str = ""
    role: str = "user"
    scope: list[str] = Field(default_factory=lambda: ["read"])
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
    chunk_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class SearchResult(BaseModel):
    content: str
    title: str
    path: str = ""
    source_path: str = ""
    doc_id: str = ""
    chunk_index: int = 0
    total_chunks: int = 0
    score: float = 0.0


class AdminAccount(BaseModel):
    username: str
    password_hash: str
    role: Literal["super_admin", "admin"] = "admin"
    created_at: str

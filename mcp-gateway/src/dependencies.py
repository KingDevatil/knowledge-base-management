"""FastAPI dependency injection helpers — request → app.state."""

from fastapi import Request

from tools import KnowledgeTools
from auth import APIKeyAuth
from admin_auth import AdminAuth
from knowledge_base import KnowledgeBase
from source_store import SourceStore
from embedding import EmbeddingProvider
from lock import WriteLock


def get_tools(request: Request) -> KnowledgeTools:
    return request.app.state.tools


def get_api_key_auth(request: Request) -> APIKeyAuth:
    return request.app.state.api_key_auth


def get_admin_auth(request: Request) -> AdminAuth:
    return request.app.state.admin_auth


def get_kb(request: Request) -> KnowledgeBase:
    return request.app.state.kb


def get_source_store(request: Request) -> SourceStore:
    return request.app.state.source_store


def get_embedder(request: Request) -> EmbeddingProvider:
    return request.app.state.embedder


def get_write_lock(request: Request) -> WriteLock:
    return request.app.state.write_lock

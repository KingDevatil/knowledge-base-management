"""MCP Streamable HTTP tests for the /mcp endpoint."""
import asyncio
import hashlib
import json
import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis as fr
import pytest
from fastapi.testclient import TestClient
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["DEBUG"] = "true"
os.environ["KBDATA_DIR"] = os.path.join(_ROOT, "kbdata")
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["SESSION_SECRET"] = "a" * 32

from auth import APIKeyAuth
from knowledge_base import KnowledgeBase
from main import app
from server import create_mcp_server
from tools import KnowledgeTools


TEST_KEY = "sk-mcp-tdd-test-key-00000"
INIT_MSG = json.dumps({
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "tdd-test", "version": "1.0"},
    },
    "id": 1,
})


class FakeLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def _seed_api_key(redis_client, auth: APIKeyAuth) -> None:
    key_hash = hashlib.sha256(TEST_KEY.encode()).hexdigest()
    await redis_client.hset(f"api_key:{key_hash}", mapping={
        "key_prefix": TEST_KEY[:10],
        "applicant": "tdd-test",
        "applicant_note": "",
        "role": "user",
        "scope": '["read"]',
        "rate_limit": "999",
        "status": "active",
        "duration": "permanent",
        "created_at": "2025-01-01T00:00:00",
        "expires_at": "",
        "use_count": "0",
        "last_used_at": "",
        "created_by": "tdd",
    })
    await auth._load_keys_to_redis()


@asynccontextmanager
async def mcp_test_lifespan(test_app):
    redis_client = fr.FakeRedis()
    auth = APIKeyAuth(redis_client, "D:/kimicode/knowledge-base-management/kbdata/config/api_keys.json")
    await _seed_api_key(redis_client, auth)

    kb = KnowledgeBase(MagicMock(), "mcp-test")
    kb.set_redis(redis_client)
    embedder = MagicMock()
    embedder.embed = AsyncMock(
        side_effect=lambda texts: [[0.1] * 1024] * (len(texts) if isinstance(texts, list) else 1)
    )
    embedder.embed_single = AsyncMock(return_value=[0.1] * 1024)
    source_store = MagicMock()
    source_store.save_source.return_value = "test-source.md"

    test_app.state.redis = redis_client
    test_app.state.api_key_auth = auth
    test_app.state.admin_auth = MagicMock()
    test_app.state.admin_auth.verify_session = AsyncMock(
        return_value={"username": "admin", "role": "super_admin"}
    )
    test_app.state.kb = kb
    test_app.state.tools = KnowledgeTools(kb, source_store, embedder, FakeLock(), auth)
    test_app.state.mcp_server = create_mcp_server(test_app.state.tools)
    test_app.state.mcp_session_manager = StreamableHTTPSessionManager(
        app=test_app.state.mcp_server,
        json_response=True,
        stateless=False,
    )

    async with test_app.state.mcp_session_manager.run():
        yield

    await redis_client.aclose()


@pytest.fixture
def client():
    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = mcp_test_lifespan
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.router.lifespan_context = original_lifespan


def test_post_mcp_with_valid_key_returns_success(client):
    response = client.post(
        "/mcp",
        content=INIT_MSG,
        headers={
            "X-API-Key": TEST_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code == 200


def test_post_mcp_with_bearer_token_returns_success(client):
    response = client.post(
        "/mcp",
        content=INIT_MSG,
        headers={
            "Authorization": f"Bearer {TEST_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code == 200


def test_post_mcp_with_conflicting_auth_headers_returns_401(client):
    response = client.post(
        "/mcp",
        content=INIT_MSG,
        headers={
            "Authorization": f"Bearer {TEST_KEY}",
            "X-API-Key": "sk-different-key",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code == 401


def test_post_mcp_without_key_returns_401(client):
    response = client.post(
        "/mcp",
        content=INIT_MSG,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code == 401


def test_get_mcp_with_valid_key_does_not_500(client):
    response = client.get("/mcp", headers={"X-API-Key": TEST_KEY})
    assert response.status_code != 500

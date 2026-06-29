import os
import sys
from types import SimpleNamespace

os.environ["DEBUG"] = "true"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from admin import routes_pages


class FakeTemplates:
    def TemplateResponse(self, request, template_name, context):
        return {"template": template_name, "context": context}


class FakeCollection:
    def get(self, include=None):
        return {"metadatas": []}


class FakeKB:
    def __init__(self):
        self.collection = FakeCollection()

    async def _doc_index_all(self):
        return []


class FakeSourceStore:
    pass


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hset(self, key, mapping):
        self.hashes[key] = dict(mapping)


class FakeRequest:
    def __init__(self, body=None, redis=None):
        self._body = body or {}
        self.app = SimpleNamespace(state=SimpleNamespace(redis=redis))

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_maintenance_page_builds_context(monkeypatch):
    monkeypatch.setattr(routes_pages, "templates", FakeTemplates())
    tools = SimpleNamespace(
        ingestion_tasks={
            "task-1": {"task_id": "task-1", "status": "failed", "started_at": "2026-01-01T00:00:00Z"},
        },
        cleanup_tasks={
            "cleanup-1": {"task_id": "cleanup-1", "status": "pending", "created_at": "2026-01-01T00:00:00Z"},
        },
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                tools=tools,
                kb=FakeKB(),
                source_store=FakeSourceStore(),
            )
        )
    )

    response = await routes_pages.maintenance_page(
        request=request,
        user={"username": "admin", "role": "admin"},
    )

    assert response["template"] == "maintenance.html"
    assert response["context"]["consistency"]["success"] is True
    assert response["context"]["ingestion_tasks"][0]["task_id"] == "task-1"
    assert response["context"]["cleanup_tasks"][0]["task_id"] == "cleanup-1"


@pytest.mark.asyncio
async def test_settings_page_builds_module_context(monkeypatch):
    monkeypatch.setattr(routes_pages, "templates", FakeTemplates())
    redis = FakeRedis()
    redis.values["kb:config:graph:semantic_threshold"] = "0.45"
    redis.hashes[routes_pages.DDNS_CONFIG_KEY] = {
        "enabled": "true",
        "provider": "dnspod",
        "domain": "example.com",
        "record_name": "kb",
        "api_token": "secret-token",
    }
    request = FakeRequest(redis=redis)

    response = await routes_pages.settings_page(
        request=request,
        user={"username": "admin", "role": "admin"},
    )

    assert response["template"] == "settings.html"
    assert response["context"]["graph_semantic_threshold"] == 0.45
    assert response["context"]["ddns_config"]["provider"] == "dnspod"
    assert response["context"]["ddns_config"]["api_token"] == ""
    assert response["context"]["ddns_has_token"] is True


@pytest.mark.asyncio
async def test_save_ddns_settings_persists_to_redis():
    redis = FakeRedis()
    request = FakeRequest(
        redis=redis,
        body={
            "enabled": True,
            "provider": "cloudflare",
            "domain": "example.com",
            "record_name": "kb",
            "record_type": "A",
            "ttl": 120,
            "endpoint": "",
            "access_key": "account",
            "api_token": "secret",
        },
    )

    response = await routes_pages.save_ddns_settings(
        request=request,
        user={"username": "admin", "role": "admin"},
    )

    assert response.status_code == 200
    assert redis.hashes[routes_pages.DDNS_CONFIG_KEY]["enabled"] == "true"
    assert redis.hashes[routes_pages.DDNS_CONFIG_KEY]["ttl"] == "120"
    assert redis.hashes[routes_pages.DDNS_CONFIG_KEY]["api_token"] == "secret"

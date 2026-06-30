import os
import sys
from types import SimpleNamespace

os.environ["DEBUG"] = "true"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from admin import routes_pages
import ddns


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
    def __init__(self, body=None, redis=None, admin_auth=None):
        self._body = body or {}
        self.app = SimpleNamespace(state=SimpleNamespace(redis=redis, admin_auth=admin_auth))

    async def json(self):
        return self._body


class FakeAdminAuth:
    def __init__(self):
        self.accounts = {
            "admin": {"username": "admin", "password_hash": "login-hash", "role": "admin"}
        }

    def _load_accounts(self):
        return self.accounts

    def _save_accounts(self, accounts):
        self.accounts = accounts
        return True

    def hash_password(self, password):
        return f"hashed:{password}"

    def verify_password(self, password, password_hash):
        return password_hash == f"hashed:{password}"


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
    await ddns.save_service(redis, {
        "enabled": True,
        "provider": "dnspod",
        "domain": "example.com",
        "record_name": "kb",
        "access_key": "token-id",
        "api_token": "secret-token",
        "ipv4_enabled": True,
        "ipv6_enabled": False,
    })
    request = FakeRequest(redis=redis, admin_auth=FakeAdminAuth())

    response = await routes_pages.settings_page(
        request=request,
        user={"username": "admin", "role": "admin"},
    )

    assert response["template"] == "settings.html"
    assert response["context"]["graph_semantic_threshold"] == 0.45
    assert response["context"]["ddns_services"][0]["provider"] == "dnspod"
    assert "api_token" not in response["context"]["ddns_services"][0]
    assert response["context"]["ddns_services"][0]["has_token"] is True
    assert "env_profiles" in response["context"]
    assert response["context"]["has_management_password"] is False


@pytest.mark.asyncio
async def test_settings_page_tolerates_optional_settings_failures(monkeypatch):
    monkeypatch.setattr(routes_pages, "templates", FakeTemplates())
    monkeypatch.setattr(routes_pages, "list_profiles", lambda: (_ for _ in ()).throw(RuntimeError("profiles unavailable")))
    monkeypatch.setattr(routes_pages, "list_reverse_proxy_configs", lambda: (_ for _ in ()).throw(RuntimeError("reverse proxy unavailable")))
    monkeypatch.setattr(routes_pages, "read_env", lambda: (_ for _ in ()).throw(RuntimeError("env unavailable")))

    class BrokenAdminAuth(FakeAdminAuth):
        def _load_accounts(self):
            raise RuntimeError("accounts unavailable")

    request = FakeRequest(redis=None, admin_auth=BrokenAdminAuth())

    response = await routes_pages.settings_page(
        request=request,
        user={"role": "admin"},
    )

    assert response["template"] == "settings.html"
    assert response["context"]["env_profiles"] == []
    assert response["context"]["reverse_proxy_configs"] == []
    assert response["context"]["reverse_proxy_service_state"]["enabled"] is False
    assert response["context"]["has_management_password"] is False


@pytest.mark.asyncio
async def test_save_ddns_settings_persists_service_to_redis():
    redis = FakeRedis()
    request = FakeRequest(
        redis=redis,
        body={
            "enabled": True,
            "provider": "cloudflare",
            "domain": "example.com",
            "record_name": "kb",
            "ttl": 120,
            "update_interval_minutes": 10,
            "endpoint": "",
            "access_key": "account",
            "api_token": "secret",
            "ipv4_enabled": True,
            "ipv6_enabled": True,
        },
    )

    response = await routes_pages.save_ddns_settings(
        request=request,
        user={"username": "admin", "role": "admin"},
    )

    assert response.status_code == 200
    services = await ddns.list_services(redis)
    assert services[0]["enabled"] is True
    assert services[0]["ttl"] == 120
    assert services[0]["update_interval_minutes"] == 10
    assert services[0]["api_token"] == "secret"
    assert services[0]["ipv6_enabled"] is True


def test_set_management_password_updates_account_file(tmp_path):
    auth = FakeAdminAuth()
    request = FakeRequest(admin_auth=auth)

    ok, msg = routes_pages._set_management_password(request, "admin", "restart-pass")

    assert ok, msg
    assert auth.accounts["admin"]["management_password_hash"] == "hashed:restart-pass"
    assert routes_pages._verify_management_password(request, "admin", "restart-pass") is True

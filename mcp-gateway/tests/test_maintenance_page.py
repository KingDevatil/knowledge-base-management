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

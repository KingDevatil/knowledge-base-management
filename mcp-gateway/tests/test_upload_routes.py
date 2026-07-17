import io
import json
import os
import sys
import zipfile
from types import SimpleNamespace

os.environ["DEBUG"] = "true"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi import UploadFile

from admin.routes_documents_api import (
    api_batch_upload,
    api_cleanup_tasks,
    api_ingestion_tasks,
    api_retry_cleanup_task,
    api_retry_ingestion_task,
    api_preview_archive,
    api_upload_archive,
    upload_submit,
)


class FakeTools:
    def __init__(self):
        self.calls = []
        self.retry_calls = []
        self.cleanup_tasks = {}
        self.cleanup_retry_calls = []

    async def import_markdown(self, title, markdown_content, path="", tags=None, created_by="system"):
        self.calls.append({
            "title": title,
            "content": markdown_content,
            "path": path,
            "tags": tags or [],
            "created_by": created_by,
        })
        index = len(self.calls)
        return {"success": True, "doc_id": f"doc-{index}", "task_id": f"task-{index}"}

    async def retry_ingestion_task(self, task_id, retried_by="system"):
        self.retry_calls.append({"task_id": task_id, "retried_by": retried_by})
        return {
            "success": True,
            "doc_id": "doc-retry",
            "task_id": "task-retry",
            "retried_from": task_id,
        }

    def retry_cleanup_task(self, task_id):
        self.cleanup_retry_calls.append(task_id)
        return {"success": True, "task_id": task_id, "status": "succeeded"}


def make_request(tools):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(tools=tools)),
        headers={"accept": "application/json"},
    )


def make_upload(filename, content):
    if isinstance(content, str):
        content = content.encode("utf-8")
    return UploadFile(filename=filename, file=io.BytesIO(content))


@pytest.mark.asyncio
async def test_api_batch_upload_returns_task_ids():
    tools = FakeTools()

    response = await api_batch_upload(
        request=make_request(tools),
        files=[make_upload("guide.md", "# Guide")],
        path="docs",
        tags="a,b",
        user={"username": "admin"},
    )
    payload = json.loads(response.body)

    assert payload["results"][0]["doc_id"] == "doc-1"
    assert payload["results"][0]["task_id"] == "task-1"
    assert payload["tasks"] == ["task-1"]
    assert tools.calls[0]["path"] == "docs"
    assert tools.calls[0]["tags"] == ["a", "b"]


@pytest.mark.asyncio
async def test_api_batch_upload_converts_csv_to_searchable_records():
    tools = FakeTools()

    response = await api_batch_upload(
        request=make_request(tools),
        files=[make_upload("inventory.csv", "商品,库存\n键盘,12\n")],
        path="docs",
        tags="inventory",
        user={"username": "admin"},
    )
    payload = json.loads(response.body)

    assert payload["results"][0]["status"] == "ok"
    assert tools.calls[0]["title"] == "inventory"
    assert "- 商品: 键盘" in tools.calls[0]["content"]
    assert "- 库存: 12" in tools.calls[0]["content"]


@pytest.mark.asyncio
async def test_api_ingestion_tasks_lists_recent_tasks_first():
    tools = SimpleNamespace(
        ingestion_tasks={
            "old": {"task_id": "old", "started_at": "2026-01-01T00:00:00Z"},
            "new": {"task_id": "new", "started_at": "2026-06-01T00:00:00Z"},
        }
    )

    response = await api_ingestion_tasks(
        request=make_request(tools),
        limit=1,
        user={"username": "admin"},
    )
    payload = json.loads(response.body)

    assert payload["total"] == 2
    assert payload["limit"] == 1
    assert [task["task_id"] for task in payload["tasks"]] == ["new"]


@pytest.mark.asyncio
async def test_api_retry_ingestion_task_delegates_to_tools():
    tools = FakeTools()

    response = await api_retry_ingestion_task(
        request=make_request(tools),
        task_id="failed-task",
        user={"username": "admin"},
    )
    payload = json.loads(response.body)

    assert payload["success"] is True
    assert payload["retried_from"] == "failed-task"
    assert tools.retry_calls == [{"task_id": "failed-task", "retried_by": "admin"}]


@pytest.mark.asyncio
async def test_api_cleanup_tasks_lists_recent_tasks_first():
    tools = FakeTools()
    tools.cleanup_tasks = {
        "old": {"task_id": "old", "created_at": "2026-01-01T00:00:00Z"},
        "new": {"task_id": "new", "created_at": "2026-06-01T00:00:00Z"},
    }

    response = await api_cleanup_tasks(
        request=make_request(tools),
        limit=1,
        user={"username": "admin"},
    )
    payload = json.loads(response.body)

    assert payload["total"] == 2
    assert [task["task_id"] for task in payload["tasks"]] == ["new"]


@pytest.mark.asyncio
async def test_api_retry_cleanup_task_delegates_to_tools():
    tools = FakeTools()

    response = await api_retry_cleanup_task(
        request=make_request(tools),
        task_id="cleanup-1",
        user={"username": "admin"},
    )
    payload = json.loads(response.body)

    assert payload["success"] is True
    assert tools.cleanup_retry_calls == ["cleanup-1"]


@pytest.mark.asyncio
async def test_api_upload_archive_returns_task_ids(tmp_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("nested/guide.md", "# Guide")
    archive.seek(0)
    tools = FakeTools()

    response = await api_upload_archive(
        request=make_request(tools),
        file=UploadFile(filename="docs.zip", file=archive),
        path="base",
        tags="tag",
        user={"username": "admin"},
    )
    payload = json.loads(response.body)

    assert payload["success"] == 1
    assert payload["results"][0]["path"] == "base/nested"
    assert payload["results"][0]["task_id"] == "task-1"
    assert payload["tasks"] == ["task-1"]


@pytest.mark.asyncio
async def test_archive_upload_and_preview_include_csv_files():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("nested/stock.csv", "名称,数量\n键盘,12\n")
    archive.seek(0)
    tools = FakeTools()

    preview = await api_preview_archive(
        request=make_request(tools),
        file=UploadFile(filename="docs.zip", file=io.BytesIO(archive.getvalue())),
        user={"username": "admin"},
    )
    preview_payload = json.loads(preview.body)
    assert preview_payload["total"] == 1
    assert preview_payload["files"][0]["filename"] == "stock.csv"

    response = await api_upload_archive(
        request=make_request(tools),
        file=UploadFile(filename="docs.zip", file=io.BytesIO(archive.getvalue())),
        path="base",
        tags="tag",
        user={"username": "admin"},
    )
    payload = json.loads(response.body)

    assert payload["success"] == 1
    assert payload["results"][0]["filename"] == "stock.csv"
    assert "- 名称: 键盘" in tools.calls[0]["content"]


@pytest.mark.asyncio
async def test_html_upload_form_accepts_csv():
    tools = FakeTools()

    response = await upload_submit(
        request=make_request(tools),
        files=[make_upload("inventory.csv", "商品,库存\n键盘,12\n")],
        path="docs",
        tags="inventory",
        existing_path="",
        user={"username": "admin"},
    )

    assert response.status_code == 302
    assert "- 商品: 键盘" in tools.calls[0]["content"]

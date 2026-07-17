import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

os.environ.setdefault("DEBUG", "true")

from server import create_mcp_server


@pytest.mark.asyncio
async def test_search_tool_schema_describes_hybrid_retrieval_and_bounds_top_k():
    server = create_mcp_server(MagicMock())

    response = await server.request_handlers[ListToolsRequest](ListToolsRequest())

    search_tool = next(tool for tool in response.root.tools if tool.name == "search_knowledge")
    top_k = search_tool.inputSchema["properties"]["top_k"]
    context_budget = search_tool.inputSchema["properties"]["max_context_chars"]
    assert "混合检索" in search_tool.description
    assert top_k["minimum"] == 1
    assert top_k["maximum"] == 50
    assert search_tool.inputSchema["properties"]["query"]["minLength"] == 1
    assert search_tool.inputSchema["properties"]["include_context"]["default"] is True
    assert context_budget["minimum"] == 0
    assert context_budget["maximum"] == 20000


@pytest.mark.asyncio
async def test_successful_tool_call_exposes_structured_content():
    tools = MagicMock()
    tools.list_directories = AsyncMock(return_value={"tree": []})
    server = create_mcp_server(tools)
    request = CallToolRequest(
        params=CallToolRequestParams(name="list_directories", arguments={})
    )

    with patch("server.require_mcp_tool_scope"):
        response = await server.request_handlers[CallToolRequest](request)

    assert response.root.isError is False
    assert response.root.structuredContent == {"tree": []}


@pytest.mark.asyncio
async def test_unknown_tool_is_reported_as_mcp_error():
    server = create_mcp_server(MagicMock())
    request = CallToolRequest(
        params=CallToolRequestParams(name="missing_tool", arguments={})
    )

    response = await server.request_handlers[CallToolRequest](request)

    assert response.root.isError is True
    assert response.root.structuredContent == {"error": "未知工具: missing_tool"}


@pytest.mark.asyncio
async def test_add_document_forwards_stage_updates_as_mcp_progress_notifications():
    session = SimpleNamespace(send_progress_notification=AsyncMock())
    request_context = SimpleNamespace(
        meta=SimpleNamespace(progressToken="progress-1"),
        session=session,
    )

    async def add_document(title, content, path="", tags=None, progress_callback=None):
        assert progress_callback is not None
        await progress_callback(35, "生成向量")
        return {"success": True, "doc_id": "doc-1", "task_id": "task-1"}

    tools = MagicMock()
    tools.add_document = add_document
    server = create_mcp_server(tools)
    request = CallToolRequest(
        params=CallToolRequestParams(
            name="add_document",
            arguments={"title": "Progress", "content": "# Content"},
        )
    )

    with (
        patch("server.require_mcp_tool_scope"),
        patch.object(type(server), "request_context", new_callable=PropertyMock) as context,
    ):
        context.return_value = request_context
        response = await server.request_handlers[CallToolRequest](request)

    assert response.root.isError is False
    session.send_progress_notification.assert_any_await(
        progress_token="progress-1",
        progress=35,
        total=100,
        message="生成向量",
    )


@pytest.mark.asyncio
async def test_search_forwards_wait_and_retrieval_updates_as_mcp_progress_notifications():
    session = SimpleNamespace(send_progress_notification=AsyncMock())
    request_context = SimpleNamespace(
        meta=SimpleNamespace(progressToken="progress-search"),
        session=session,
    )

    async def search_knowledge(
        query,
        top_k=5,
        filter_tags=None,
        filter_path="",
        include_context=True,
        max_context_chars=None,
        progress_callback=None,
    ):
        assert progress_callback is not None
        await progress_callback(5, "等待检索执行槽位")
        await progress_callback(35, "执行向量、关键词和结构混合检索")
        return {"query": query, "results": [], "total": 0, "status": "ok"}

    tools = MagicMock()
    tools.search_knowledge = search_knowledge
    server = create_mcp_server(tools)
    request = CallToolRequest(
        params=CallToolRequestParams(
            name="search_knowledge",
            arguments={"query": "progress"},
        )
    )

    with (
        patch("server.require_mcp_tool_scope"),
        patch.object(type(server), "request_context", new_callable=PropertyMock) as context,
    ):
        context.return_value = request_context
        response = await server.request_handlers[CallToolRequest](request)

    assert response.root.isError is False
    for progress, message in (
        (0, "开始执行 search_knowledge"),
        (5, "等待检索执行槽位"),
        (35, "执行向量、关键词和结构混合检索"),
        (100, "search_knowledge 执行完成"),
    ):
        session.send_progress_notification.assert_any_await(
            progress_token="progress-search",
            progress=progress,
            total=100,
            message=message,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["update_document", "reindex_document"])
async def test_existing_document_writes_forward_mcp_progress_notifications(tool_name):
    session = SimpleNamespace(send_progress_notification=AsyncMock())
    request_context = SimpleNamespace(
        meta=SimpleNamespace(progressToken=f"progress-{tool_name}"),
        session=session,
    )

    async def update_document(
        doc_id,
        title,
        content,
        path="",
        tags=None,
        progress_callback=None,
    ):
        assert progress_callback is not None
        await progress_callback(35, "生成向量")
        return {"success": True, "doc_id": doc_id}

    async def reindex_document(doc_id, progress_callback=None):
        assert progress_callback is not None
        await progress_callback(75, "替换文档索引")
        return {"success": True, "doc_id": doc_id}

    tools = MagicMock()
    tools.update_document = update_document
    tools.reindex_document = reindex_document
    server = create_mcp_server(tools)
    arguments = {"doc_id": "doc-1"}
    if tool_name == "update_document":
        arguments.update({"title": "Updated", "content": "# Updated"})
    request = CallToolRequest(
        params=CallToolRequestParams(name=tool_name, arguments=arguments)
    )

    with (
        patch("server.require_mcp_tool_scope"),
        patch.object(type(server), "request_context", new_callable=PropertyMock) as context,
    ):
        context.return_value = request_context
        response = await server.request_handlers[CallToolRequest](request)

    assert response.root.isError is False
    expected = (35, "生成向量") if tool_name == "update_document" else (75, "替换文档索引")
    session.send_progress_notification.assert_any_await(
        progress_token=f"progress-{tool_name}",
        progress=expected[0],
        total=100,
        message=expected[1],
    )

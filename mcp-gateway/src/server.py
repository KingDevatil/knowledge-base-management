import json
from typing import Any

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool

from config import get_settings
from tools import KnowledgeTools


def create_mcp_server(tools: KnowledgeTools) -> Server:
    """创建 MCP 服务器实例"""
    settings = get_settings()
    server = Server(settings.APP_NAME)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="search_knowledge",
                description="向量检索知识库，返回与查询最相关的文档片段",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "查询内容"},
                        "top_k": {"type": "integer", "default": 5, "description": "返回结果数量"},
                        "filter_tags": {"type": "array", "items": {"type": "string"}, "default": [], "description": "按标签筛选"},
                        "filter_path": {"type": "string", "default": "", "description": "按目录路径筛选"},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="add_document",
                description="添加新文档到知识库",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "文档标题"},
                        "content": {"type": "string", "description": "文档内容（Markdown）"},
                        "path": {"type": "string", "default": "", "description": "所属目录路径"},
                        "tags": {"type": "array", "items": {"type": "string"}, "default": [], "description": "标签列表"},
                    },
                    "required": ["title", "content"],
                },
            ),
            Tool(
                name="get_document",
                description="获取单个文档的完整信息，包括内容、标签和所有切片",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "doc_id": {"type": "string", "description": "文档 ID"},
                    },
                    "required": ["doc_id"],
                },
            ),
            Tool(
                name="update_document",
                description="更新已有文档",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "doc_id": {"type": "string", "description": "文档 ID"},
                        "title": {"type": "string", "description": "新标题"},
                        "content": {"type": "string", "description": "新内容"},
                        "path": {"type": "string", "default": "", "description": "新目录路径"},
                        "tags": {"type": "array", "items": {"type": "string"}, "default": [], "description": "新标签列表"},
                    },
                    "required": ["doc_id", "title", "content"],
                },
            ),
            Tool(
                name="delete_document",
                description="删除文档",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "doc_id": {"type": "string", "description": "文档 ID"},
                    },
                    "required": ["doc_id"],
                },
            ),
            Tool(
                name="list_documents",
                description="列出知识库中的文档",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "tags": {"type": "array", "items": {"type": "string"}, "default": [], "description": "按标签筛选"},
                        "path": {"type": "string", "default": "", "description": "按目录路径筛选"},
                        "limit": {"type": "integer", "default": 20, "description": "每页数量"},
                        "offset": {"type": "integer", "default": 0, "description": "偏移量"},
                    },
                },
            ),
            Tool(
                name="list_directories",
                description="列出知识库的目录树结构",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="rename_directory",
                description="重命名目录，将该目录及其所有子目录下的文档移动到新路径",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "old_path": {"type": "string", "description": "当前目录路径"},
                        "new_path": {"type": "string", "description": "新目录路径"},
                    },
                    "required": ["old_path", "new_path"],
                },
            ),
            Tool(
                name="delete_directory",
                description="删除目录，将该目录及其子目录下的所有文档移至根目录",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "要删除的目录路径"},
                    },
                    "required": ["path"],
                },
            ),
            Tool(
                name="reindex_document",
                description="重新切片并向量化单个文档（用于切片策略变更后重建索引）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "doc_id": {"type": "string", "description": "文档 ID"},
                    },
                    "required": ["doc_id"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: Any) -> list[TextContent]:
        try:
            if name == "search_knowledge":
                result = await tools.search_knowledge(
                    query=arguments.get("query", ""),
                    top_k=arguments.get("top_k", 5),
                    filter_tags=arguments.get("filter_tags") or [],
                    filter_path=arguments.get("filter_path", ""),
                )
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "add_document":
                result = await tools.add_document(
                    title=arguments.get("title", ""),
                    content=arguments.get("content", ""),
                    path=arguments.get("path", ""),
                    tags=arguments.get("tags") or [],
                )
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "get_document":
                result = await tools.get_document(
                    doc_id=arguments.get("doc_id", ""),
                )
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "update_document":
                result = await tools.update_document(
                    doc_id=arguments.get("doc_id", ""),
                    title=arguments.get("title", ""),
                    content=arguments.get("content", ""),
                    path=arguments.get("path", ""),
                    tags=arguments.get("tags") or [],
                )
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "delete_document":
                result = await tools.delete_document(
                    doc_id=arguments.get("doc_id", ""),
                )
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "list_documents":
                result = await tools.list_documents(
                    tags=arguments.get("tags") or [],
                    path=arguments.get("path", ""),
                    limit=arguments.get("limit", 20),
                    offset=arguments.get("offset", 0),
                )
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "list_directories":
                result = await tools.list_directories()
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "rename_directory":
                result = await tools.rename_directory(
                    old_path=arguments.get("old_path", ""),
                    new_path=arguments.get("new_path", ""),
                )
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "delete_directory":
                result = await tools.delete_directory(
                    path=arguments.get("path", ""),
                )
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "reindex_document":
                result = await tools.reindex_document(
                    doc_id=arguments.get("doc_id", ""),
                )
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            else:
                return [TextContent(type="text", text=json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False))]

        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]

    return server

import json
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from config import get_settings
from kb_graph import KnowledgeGraphBuilder
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
                        "path": {"type": "string", "default": "", "description": "新目录路径（留空则保留原路径）"},
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
            Tool(
                name="build_knowledge_graph",
                description="构建知识图谱：分析文档关系（标签共享、目录结构、语义相似），生成交互式可视化图谱",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "semantic_threshold": {
                            "type": "number",
                            "default": 0.0,
                            "description": "语义相似度阈值（0.0=关闭，0.7=推荐开启值）。开启后对所有文档计算向量相似度，超过阈值的文档对之间添加边。文档数较多时耗时较长。",
                        },
                    },
                },
            ),
        ]

    def _make_result(result: dict) -> list[TextContent]:
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    _DISPATCH = {
        "search_knowledge": lambda a, t: t.search_knowledge(
            query=a.get("query", ""), top_k=a.get("top_k", 5),
            filter_tags=a.get("filter_tags") or [], filter_path=a.get("filter_path", ""),
        ),
        "add_document": lambda a, t: t.add_document(
            title=a.get("title", ""), content=a.get("content", ""),
            path=a.get("path", ""), tags=a.get("tags") or [],
        ),
        "get_document": lambda a, t: t.get_document(doc_id=a.get("doc_id", "")),
        "update_document": lambda a, t: t.update_document(
            doc_id=a.get("doc_id", ""), title=a.get("title", ""),
            content=a.get("content", ""), path=a.get("path", ""), tags=a.get("tags") or [],
        ),
        "delete_document": lambda a, t: t.delete_document(doc_id=a.get("doc_id", "")),
        "list_documents": lambda a, t: t.list_documents(
            tags=a.get("tags") or [], path=a.get("path", ""),
            limit=a.get("limit", 20), offset=a.get("offset", 0),
        ),
        "list_directories": lambda a, t: t.list_directories(),
        "rename_directory": lambda a, t: t.rename_directory(
            old_path=a.get("old_path", ""), new_path=a.get("new_path", ""),
        ),
        "delete_directory": lambda a, t: t.delete_directory(path=a.get("path", "")),
        "reindex_document": lambda a, t: t.reindex_document(doc_id=a.get("doc_id", "")),
        "build_knowledge_graph": lambda a, t: KnowledgeGraphBuilder(
            kb=t.kb, embedder=getattr(t, "embedder", None)
        ).build(semantic_threshold=float(a.get("semantic_threshold", 0.0))),
    }

    @server.call_tool()
    async def call_tool(name: str, arguments: Any) -> list[TextContent]:
        handler = _DISPATCH.get(name)
        if handler is None:
            return _make_result({"error": f"未知工具: {name}"})
        try:
            result = await handler(arguments, tools)
            return _make_result(result)
        except Exception as e:
            return _make_result({"error": str(e)})

    return server

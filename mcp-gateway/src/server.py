import json
from typing import Any

from fastapi import HTTPException, status
from mcp.server import Server
from mcp.types import CallToolResult, TextContent, Tool

from config import get_settings
from kb_graph import KnowledgeGraphBuilder
from mcp_auth_context import get_mcp_api_key_info
from tools import KnowledgeTools
from path_permissions import has_path_access
from audit_log import actor_from_api_key


MCP_TOOL_METADATA = {
    "search_knowledge": {"required_scope": "read", "category": "read", "risk_level": "low"},
    "get_document": {"required_scope": "read", "category": "read", "risk_level": "low"},
    "list_documents": {"required_scope": "read", "category": "read", "risk_level": "low"},
    "list_directories": {"required_scope": "read", "category": "read", "risk_level": "low"},
    "add_document": {"required_scope": "write", "category": "write", "risk_level": "high"},
    "update_document": {"required_scope": "write", "category": "write", "risk_level": "high"},
    "delete_document": {"required_scope": "write", "category": "write", "risk_level": "high"},
    "rename_directory": {"required_scope": "write", "category": "write", "risk_level": "high"},
    "delete_directory": {"required_scope": "write", "category": "write", "risk_level": "high"},
    "reindex_document": {"required_scope": "write", "category": "write", "risk_level": "medium"},
    "list_document_versions": {"required_scope": "read", "category": "read", "risk_level": "low"},
    "restore_document_version": {"required_scope": "write", "category": "write", "risk_level": "high"},
    "find_similar_documents": {"required_scope": "read", "category": "read", "risk_level": "low"},
    "upsert_document": {"required_scope": "write", "category": "write", "risk_level": "high"},
    "build_knowledge_graph": {"required_scope": "write", "category": "admin", "risk_level": "medium"},
}


def require_mcp_tool_scope(tool_name: str) -> None:
    metadata = MCP_TOOL_METADATA.get(tool_name)
    if metadata is None:
        return

    required_scope = metadata["required_scope"]
    api_key_info = get_mcp_api_key_info()
    if api_key_info is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MCP 工具调用缺少认证上下文",
        )
    if required_scope not in api_key_info.scope:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"MCP 工具 {tool_name} 需要 {required_scope} 权限",
        )


def create_mcp_server(tools: KnowledgeTools) -> Server:
    """创建 MCP 服务器实例"""
    settings = get_settings()
    server = Server(settings.APP_NAME)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="search_knowledge",
                description="混合检索知识库（向量、关键词、结构），返回与查询最相关且带引用上下文的文档片段",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "description": "查询内容"},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5, "description": "返回结果数量"},
                        "filter_tags": {"type": "array", "items": {"type": "string"}, "default": [], "description": "按标签筛选"},
                        "filter_path": {"type": "string", "default": "", "description": "按目录路径筛选"},
                        "include_context": {"type": "boolean", "default": True, "description": "是否返回命中切片前后的相邻上下文"},
                        "max_context_chars": {"type": "integer", "minimum": 0, "maximum": 20000, "description": "每条结果的相邻上下文总字符预算；默认由服务配置决定"},
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
                        "title": {"type": "string", "minLength": 1, "description": "文档标题"},
                        "content": {"type": "string", "minLength": 1, "description": "文档内容（Markdown）"},
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
                        "doc_id": {"type": "string", "minLength": 1, "description": "文档 ID"},
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
                        "doc_id": {"type": "string", "minLength": 1, "description": "文档 ID"},
                        "title": {"type": "string", "minLength": 1, "description": "新标题"},
                        "content": {"type": "string", "minLength": 1, "description": "新内容"},
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
                        "doc_id": {"type": "string", "minLength": 1, "description": "文档 ID"},
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
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 20, "description": "每页数量"},
                        "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "偏移量"},
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
                        "old_path": {"type": "string", "minLength": 1, "description": "当前目录路径"},
                        "new_path": {"type": "string", "minLength": 1, "description": "新目录路径"},
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
                        "path": {"type": "string", "minLength": 1, "description": "要删除的目录路径"},
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
                        "doc_id": {"type": "string", "minLength": 1, "description": "文档 ID"},
                    },
                    "required": ["doc_id"],
                },
            ),
            Tool(
                name="list_document_versions",
                description="List document version snapshots for rollback.",
                inputSchema={
                    "type": "object",
                    "properties": {"doc_id": {"type": "string", "minLength": 1, "description": "Document ID"}},
                    "required": ["doc_id"],
                },
            ),
            Tool(
                name="restore_document_version",
                description="Restore a document to a previous version snapshot.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "doc_id": {"type": "string", "minLength": 1, "description": "Document ID"},
                        "version_id": {"type": "string", "minLength": 1, "description": "Version ID"},
                    },
                    "required": ["doc_id", "version_id"],
                },
            ),
            Tool(
                name="find_similar_documents",
                description="Find same-title, same-content, or semantically similar documents before writing.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 1},
                        "content": {"type": "string", "minLength": 1},
                        "path": {"type": "string", "default": ""},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                    },
                    "required": ["title", "content"],
                },
            ),
            Tool(
                name="upsert_document",
                description="Create or update a document, preferring existing same-path same-title documents.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 1},
                        "content": {"type": "string", "minLength": 1},
                        "path": {"type": "string", "default": ""},
                        "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                        "match_strategy": {"type": "string", "enum": ["title_path", "hash", "semantic"], "default": "title_path"},
                        "on_conflict": {"type": "string", "enum": ["update", "skip", "create_new"], "default": "update"},
                    },
                    "required": ["title", "content"],
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
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "default": 0.0,
                            "description": "语义相似度阈值（0.0=关闭，0.7=推荐开启值）。开启后对所有文档计算向量相似度，超过阈值的文档对之间添加边。文档数较多时耗时较长。",
                        },
                    },
                },
            ),
        ]

    def _make_error(result: dict) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2),
            )],
            structuredContent=result,
            isError=True,
        )

    _DISPATCH = {
        "search_knowledge": lambda a, t: t.search_knowledge(
            query=a.get("query", ""), top_k=a.get("top_k", 5),
            filter_tags=a.get("filter_tags") or [], filter_path=a.get("filter_path", ""),
            include_context=a.get("include_context", True),
            max_context_chars=a.get("max_context_chars"),
        ),
        "add_document": lambda a, t, progress: t.add_document(
            title=a.get("title", ""), content=a.get("content", ""),
            path=a.get("path", ""), tags=a.get("tags") or [],
            progress_callback=progress,
        ),
        "get_document": lambda a, t: t.get_document(doc_id=a.get("doc_id", "")),
        "update_document": lambda a, t, progress: t.update_document(
            doc_id=a.get("doc_id", ""), title=a.get("title", ""),
            content=a.get("content", ""), path=a.get("path", ""), tags=a.get("tags") or [],
            progress_callback=progress,
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
        "reindex_document": lambda a, t, progress: t.reindex_document(
            doc_id=a.get("doc_id", ""), progress_callback=progress,
        ),
        "list_document_versions": lambda a, t: t.list_document_versions(doc_id=a.get("doc_id", "")),
        "restore_document_version": lambda a, t: t.restore_document_version(
            doc_id=a.get("doc_id", ""), version_id=a.get("version_id", ""), restored_by="mcp"
        ),
        "find_similar_documents": lambda a, t: t.find_similar_documents(
            title=a.get("title", ""), content=a.get("content", ""),
            path=a.get("path", ""), top_k=a.get("top_k", 5),
        ),
        "upsert_document": lambda a, t: t.upsert_document(
            title=a.get("title", ""), content=a.get("content", ""),
            path=a.get("path", ""), tags=a.get("tags") or [],
            match_strategy=a.get("match_strategy", "title_path"),
            on_conflict=a.get("on_conflict", "update"), created_by="mcp",
        ),
        "build_knowledge_graph": lambda a, t: KnowledgeGraphBuilder(
            kb=t.kb, embedder=getattr(t, "embedder", None)
        ).build(semantic_threshold=float(a.get("semantic_threshold", 0.0))),
    }
    _PROGRESS_AWARE_TOOLS = {"add_document", "update_document", "reindex_document"}

    async def _report_progress(progress: float, message: str) -> None:
        try:
            context = server.request_context
        except LookupError:
            return
        progress_token = getattr(context.meta, "progressToken", None) if context.meta else None
        if progress_token is None:
            return
        try:
            await context.session.send_progress_notification(
                progress_token=progress_token,
                progress=progress,
                total=100,
                message=message,
            )
        except Exception:
            # Progress is advisory; notification failures must not fail the tool call.
            return

    async def _require_mcp_path_access(name: str, arguments: dict, api_key_info) -> None:
        if api_key_info is None:
            return
        path = ""
        if name in {"search_knowledge", "list_documents", "add_document", "find_similar_documents", "upsert_document"}:
            path = arguments.get("filter_path") or arguments.get("path") or ""
        elif name == "update_document":
            doc = await tools.kb._doc_index_get(arguments.get("doc_id", ""))
            old_path = str((doc or {}).get("path", ""))
            new_path = arguments.get("path") or old_path
            if not has_path_access(api_key_info, old_path) or not has_path_access(api_key_info, new_path):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API Key has no access to this path")
            return
        elif name in {"get_document", "delete_document", "reindex_document", "list_document_versions", "restore_document_version"}:
            doc = await tools.kb._doc_index_get(arguments.get("doc_id", ""))
            path = str((doc or {}).get("path", ""))
            if name == "restore_document_version":
                try:
                    version = tools.version_store.get_version(arguments.get("doc_id", ""), arguments.get("version_id", ""))
                    version_path = str(version.get("path", ""))
                except Exception:
                    version_path = path
                if not has_path_access(api_key_info, path) or not has_path_access(api_key_info, version_path):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API Key has no access to this path")
                return
        elif name in {"rename_directory", "delete_directory"}:
            path = arguments.get("old_path") or arguments.get("path") or ""
        if not has_path_access(api_key_info, path):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API Key has no access to this path")

    def _audit_arguments(arguments: dict) -> dict:
        safe_args = dict(arguments)
        for field in ("content", "markdown_content"):
            if field in safe_args:
                safe_args[f"{field}_length"] = len(str(safe_args.get(field) or ""))
                safe_args[field] = "[redacted]"
        return safe_args

    @server.call_tool()
    async def call_tool(name: str, arguments: Any) -> dict | CallToolResult:
        arguments = arguments or {}
        handler = _DISPATCH.get(name)
        if handler is None:
            return _make_error({"error": f"未知工具: {name}"})
        api_key_info = get_mcp_api_key_info()
        actor_type, actor_id = actor_from_api_key(api_key_info)
        try:
            require_mcp_tool_scope(name)
            await _require_mcp_path_access(name, arguments, api_key_info)
            if name in _PROGRESS_AWARE_TOOLS:
                await _report_progress(0, f"开始执行 {name}")
                result = await handler(arguments, tools, _report_progress)
                await _report_progress(100, f"{name} 执行完成")
            else:
                result = await handler(arguments, tools)
            meta = MCP_TOOL_METADATA.get(name, {})
            if meta.get("category") != "read":
                tools.audit_logger.log(
                    action=f"mcp.{name}",
                    actor_type=actor_type,
                    actor=actor_id,
                    target_type="mcp_tool",
                    target_id=str(arguments.get("doc_id") or arguments.get("path") or arguments.get("title") or ""),
                    detail={"arguments": _audit_arguments(arguments), "risk_level": meta.get("risk_level", "")},
                    success=True,
                )
            return result
        except HTTPException as e:
            tools.audit_logger.log(
                action=f"mcp.{name}",
                actor_type=actor_type,
                actor=actor_id,
                target_type="mcp_tool",
                target_id=str(arguments.get("doc_id") or arguments.get("path") or arguments.get("title") or ""),
                detail={"arguments": _audit_arguments(arguments), "status_code": e.status_code, "error": e.detail},
                success=False,
            )
            return _make_error({"error": e.detail, "status_code": e.status_code})
        except Exception as e:
            tools.audit_logger.log(
                action=f"mcp.{name}",
                actor_type=actor_type,
                actor=actor_id,
                target_type="mcp_tool",
                target_id=str(arguments.get("doc_id") or arguments.get("path") or arguments.get("title") or ""),
                detail={"arguments": _audit_arguments(arguments), "error": str(e)},
                success=False,
            )
            return _make_error({"error": str(e)})

    return server

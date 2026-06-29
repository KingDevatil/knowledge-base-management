import os
from contextlib import asynccontextmanager
from pathlib import Path

import chromadb
import redis.asyncio as redis
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import BaseRoute, Match
from starlette.types import Scope, Receive, Send

from config import get_settings
from auth import APIKeyAuth
from admin_auth import AdminAuth
from lock import WriteLock
from knowledge_base import KnowledgeBase
from source_store import SourceStore
from embedding import build_embedding_provider
from tools import KnowledgeTools
from server import create_mcp_server
from logger import setup_logger
from middleware import request_logging_middleware, csrf_middleware
from mcp_auth_context import set_mcp_api_key_info, reset_mcp_api_key_info
from api_routes import api_router
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

settings = get_settings()
logger = setup_logger()


def _create_local_source_store():
    from local_store import LocalFileStore
    import os as _os
    fallback_base = _os.path.join(settings.KBDATA_DIR, "sources") if settings.KBDATA_DIR else "kbdata/sources"
    return LocalFileStore(
        base_dir=fallback_base,
        bucket=settings.MINIO_BUCKET,
    ), fallback_base

# Windows 系统代理会导致 httpx 走代理连接本地 ChromaDB 被拒
import os as _os
_no_proxy = _os.environ.get("NO_PROXY", "")
if "localhost" not in _no_proxy:
    _os.environ["NO_PROXY"] = ",".join(filter(None, [_no_proxy, "localhost,127.0.0.1"]))

# ---------- Lifespan 管理 ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting MCP Gateway...")
    # 启动：初始化所有连接
    app.state.redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
    app.state.chroma = chromadb.HttpClient(
        host=settings.CHROMA_HOST,
        port=settings.CHROMA_PORT,
    )
    app.state.kb = KnowledgeBase(app.state.chroma, settings.CHROMA_COLLECTION)
    app.state.kb.set_redis(app.state.redis)  # 注入 Redis 用于文档索引缓存
    # 源文件存储：优先 MinIO，不可用时回退到本地文件系统
    try:
        minio_store = SourceStore(
            endpoint=settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            bucket=settings.MINIO_BUCKET,
            secure=settings.MINIO_SECURE,
        )
        local_store, fallback_base = _create_local_source_store()
        try:
            minio_docs = minio_store.list_all_documents()
            local_docs = local_store.list_all_documents()
        except Exception:
            minio_docs = []
            local_docs = []
        if settings.KBDATA_DIR and not minio_docs and local_docs:
            app.state.source_store = local_store
            logger.warning(
                "MinIO is available but empty; using LocalFileStore at "
                f"{fallback_base} with {len(local_docs)} existing source files"
            )
        else:
            app.state.source_store = minio_store
            logger.info("Source store: MinIO")
    except Exception as e:
        app.state.source_store, fallback_base = _create_local_source_store()
        logger.warning(f"MinIO unavailable ({e}), using LocalFileStore at {fallback_base}")
    app.state.embedder = build_embedding_provider(
        primary_url=settings.OLLAMA_URL,
        primary_model=settings.OLLAMA_MODEL,
        fallback_specs=settings.EMBEDDING_FALLBACKS,
        health_cache_ttl=settings.EMBEDDING_HEALTH_CACHE_TTL,
        failure_threshold=settings.EMBEDDING_FAILURE_THRESHOLD,
        circuit_cooldown=settings.EMBEDDING_CIRCUIT_COOLDOWN,
    )
    app.state.api_key_auth = APIKeyAuth(app.state.redis, settings.API_KEY_FILE)
    app.state.admin_auth = AdminAuth(
        app.state.redis,
        settings.ADMIN_ACCOUNTS_FILE,
        settings.SESSION_SECRET,
        settings.SESSION_MAX_AGE,
    )
    app.state.write_lock = WriteLock(
        app.state.redis,
        settings.WRITE_LOCK_KEY,
        settings.WRITE_LOCK_TTL,
    )
    app.state.tools = KnowledgeTools(
        kb=app.state.kb,
        source_store=app.state.source_store,
        embedder=app.state.embedder,
        write_lock=app.state.write_lock,
        api_key_auth=app.state.api_key_auth,
        redis_client=app.state.redis,
    )
    app.state.mcp_server = create_mcp_server(app.state.tools)
    app.state.sse_transport = SseServerTransport("/sse/messages/")
    # 官方 StreamableHTTP Session Manager —— 管理会话、transport 和消息路由
    app.state.mcp_session_manager = StreamableHTTPSessionManager(
        app=app.state.mcp_server,
        json_response=True,
        stateless=False,
    )

    # 将文件中的 Key 同步到 Redis（不删除 Redis 中已有 Key）
    await app.state.api_key_auth._load_keys_to_redis()

    # 启动健康检查
    startup_checks = {}
    try:
        await app.state.redis.ping()
        startup_checks["redis"] = "ok"
    except Exception as e:
        startup_checks["redis"] = f"error: {str(e)}"
        logger.warning(f"Startup check failed for Redis: {e}")

    try:
        app.state.chroma.heartbeat()
        startup_checks["chroma"] = "ok"
    except Exception as e:
        startup_checks["chroma"] = f"error: {str(e)}"
        logger.warning(f"Startup check failed for Chroma: {e}")

    try:
        ollama_ok = await app.state.embedder.health_check()
        startup_checks["ollama"] = "ok" if ollama_ok else "unavailable"
        if not ollama_ok:
            logger.warning("Startup check: Ollama embedding service unavailable")
    except Exception as e:
        startup_checks["ollama"] = f"error: {str(e)}"
        logger.warning(f"Startup check failed for Ollama: {e}")

    try:
        store = app.state.source_store
        if hasattr(store, "client"):
            store.client.bucket_exists(settings.MINIO_BUCKET)
        else:
            store.bucket_exists(settings.MINIO_BUCKET)
        startup_checks["minio"] = "ok"
    except Exception as e:
        startup_checks["minio"] = f"error: {str(e)}"
        logger.warning(f"Startup check failed for MinIO: {e}")

    logger.info(f"MCP Gateway started successfully. Startup checks: {startup_checks}")

    # 在 Session Manager 的 run() 上下文中 yield，让其 task group 管理所有会话
    async with app.state.mcp_session_manager.run():
        yield

    # 关闭
    logger.info("Shutting down MCP Gateway...")
    await app.state.redis.close()
    await app.state.embedder.close()
    logger.info("MCP Gateway stopped")


app = FastAPI(
    title=settings.APP_NAME,
    lifespan=lifespan,
    redirect_slashes=False,
)

# CORS 中间件
if settings.CORS_ORIGINS:
    origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Session 中间件 — 允许 HTTP 访问（Nginx 在生产环境处理 HTTPS）
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET,
    max_age=settings.SESSION_MAX_AGE,
    same_site="lax",
    https_only=False,
)

# Request logging + CSRF protection middleware
app.middleware("http")(request_logging_middleware)
app.middleware("http")(csrf_middleware)

# 静态文件
static_dir = os.path.join(os.path.dirname(__file__), "admin", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 知识图谱输出目录
graph_dir = Path(settings.KBDATA_DIR or "kbdata") / "graph"
graph_dir.mkdir(parents=True, exist_ok=True)
app.mount("/graph-assets", StaticFiles(directory=str(graph_dir)), name="graph-assets")


# ---------- 依赖注入辅助 ----------

from dependencies import (get_tools, get_api_key_auth, get_admin_auth,
                          get_kb, get_source_store, get_embedder, get_write_lock)


# 未登录 401 → 重定向到登录页（适用于后台管理页面）
@app.exception_handler(HTTPException)
async def admin_auth_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401 and request.url.path.startswith("/admin"):
        return RedirectResponse(url="/admin/login", status_code=302)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


# ---------- MCP Streamable HTTP 路由 ----------

class _MCPRoute(BaseRoute):
    """原始 ASGI Route：认证后委托给 StreamableHTTPSessionManager 处理"""

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        request = Request(scope, receive)
        try:
            api_key_info = await request.app.state.api_key_auth.authenticate(request)
        except HTTPException as e:
            resp = JSONResponse(status_code=e.status_code, content={"detail": e.detail})
            await resp(scope, receive, send)
            return

        # 委托给官方 Session Manager —— 它处理会话创建、transport 生命周期和消息路由
        token = set_mcp_api_key_info(api_key_info)
        try:
            await request.app.state.mcp_session_manager.handle_request(scope, receive, send)
        finally:
            reset_mcp_api_key_info(token)

    def matches(self, scope: Scope) -> tuple[Match, dict[str, str]]:
        if scope["path"] in ("/mcp", "/mcp/") and scope["method"] in ("GET", "POST", "DELETE"):
            return Match.FULL, {}
        return Match.NONE, {}


app.router.routes.insert(0, _MCPRoute())


# ---------- MCP SSE 路由 ----------

@app.get("/sse")
async def mcp_sse_endpoint(request: Request):
    """MCP SSE 连接端点 —— 支持 Header / Query 两种 API Key 认证"""
    # 如果 Query 参数有 api_key，注入到 header 供 authenticate 使用
    if "api_key" in request.query_params:
        request.headers.__dict__["_list"].append(
            (b"x-api-key", request.query_params["api_key"].encode())
        )
    api_key_auth = get_api_key_auth(request)
    api_key_info = await api_key_auth.authenticate(request)

    token = set_mcp_api_key_info(api_key_info)
    try:
        async with request.app.state.sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await request.app.state.mcp_server.run(
                read_stream,
                write_stream,
                request.app.state.mcp_server.create_initialization_options(),
            )
    finally:
        reset_mcp_api_key_info(token)


@app.post("/sse/messages/")
async def mcp_sse_messages(request: Request):
    """MCP SSE 消息端点（客户端通过 POST 发送请求到此端点）"""
    api_key_auth = get_api_key_auth(request)
    api_key_info = await api_key_auth.authenticate(request)
    token = set_mcp_api_key_info(api_key_info)
    try:
        await request.app.state.sse_transport.handle_post_message(
            request.scope, request.receive, request._send
        )
    finally:
        reset_mcp_api_key_info(token)


# ---------- REST API / Health / Metrics ----------

app.include_router(api_router)


# ---------- 导入后台管理路由 ----------

try:
    from admin.routes import router as admin_router
    app.include_router(admin_router, prefix="/admin")
    # 用户管理路由 (admin only)
    from admin.user_routes import user_router
    app.include_router(user_router, prefix="/admin")
    # 分享路由（公开，无 /admin 前缀，基于 token 鉴权）
    from admin.share_routes import share_router
    app.include_router(share_router)
except ImportError:
    pass  # 后台路由将在后续实现

# /admin 重定向到仪表盘
@app.get("/admin", include_in_schema=False)
async def admin_root():
    return RedirectResponse(url="/admin/dashboard", status_code=302)

# 根路径重定向到后台管理
@app.get("/")
async def root():
    return RedirectResponse(url="/admin/dashboard", status_code=302)

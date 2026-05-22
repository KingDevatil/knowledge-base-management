import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import chromadb
import redis.asyncio as redis
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from config import get_settings
from auth import APIKeyAuth
from admin_auth import AdminAuth
from lock import WriteLock
from knowledge_base import KnowledgeBase
from source_store import SourceStore
from embedding import OllamaEmbedder
from tools import KnowledgeTools
from server import create_mcp_server
from logger import setup_logger
from mcp.server.sse import SseServerTransport

settings = get_settings()
logger = setup_logger()
START_TIME = time.time()

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
    # 源文件存储：优先 MinIO，不可用时回退到本地文件系统
    try:
        app.state.source_store = SourceStore(
            endpoint=settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            bucket=settings.MINIO_BUCKET,
            secure=settings.MINIO_SECURE,
        )
        logger.info("Source store: MinIO")
    except Exception as e:
        from local_store import LocalFileStore
        app.state.source_store = LocalFileStore(
            base_dir="kbdata/sources",
            bucket=settings.MINIO_BUCKET,
        )
        logger.warning(f"MinIO unavailable ({e}), using LocalFileStore")
    app.state.embedder = OllamaEmbedder(settings.OLLAMA_URL, settings.OLLAMA_MODEL)
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
    )
    app.state.mcp_server = create_mcp_server(app.state.tools)
    app.state.sse_transport = SseServerTransport("/sse/messages/")

    # 加载 API Key 到 Redis
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
        app.state.source_store.client.bucket_exists(settings.MINIO_BUCKET)
        startup_checks["minio"] = "ok"
    except Exception as e:
        startup_checks["minio"] = f"error: {str(e)}"
        logger.warning(f"Startup check failed for MinIO: {e}")

    logger.info(f"MCP Gateway started successfully. Startup checks: {startup_checks}")

    yield

    # 关闭
    logger.info("Shutting down MCP Gateway...")
    await app.state.redis.close()
    await app.state.embedder.close()
    logger.info("MCP Gateway stopped")


app = FastAPI(
    title=settings.APP_NAME,
    lifespan=lifespan,
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

# Session 中间件
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET,
    max_age=settings.SESSION_MAX_AGE,
    same_site="lax",
    https_only=not settings.DEBUG,
)

# 请求日志中间件
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    client_host = request.client.host if request.client else "-"
    method = request.method
    path = request.url.path

    response = await call_next(request)

    duration = time.time() - start
    logger.info(
        f"{client_host} - \"{method} {path} HTTP/1.1\" {response.status_code} {duration:.3f}s"
    )
    return response

# 静态文件
static_dir = os.path.join(os.path.dirname(__file__), "admin", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ---------- 依赖注入辅助 ----------

def get_tools(request: Request) -> KnowledgeTools:
    return request.app.state.tools


def get_api_key_auth(request: Request) -> APIKeyAuth:
    return request.app.state.api_key_auth


def get_admin_auth(request: Request) -> AdminAuth:
    return request.app.state.admin_auth


# 未登录 401 → 重定向到登录页（适用于后台管理页面）
@app.exception_handler(HTTPException)
async def admin_auth_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401 and request.url.path.startswith("/admin"):
        return RedirectResponse(url="/admin/login", status_code=302)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


def get_kb(request: Request) -> KnowledgeBase:
    return request.app.state.kb


def get_source_store(request: Request) -> SourceStore:
    return request.app.state.source_store


def get_embedder(request: Request) -> OllamaEmbedder:
    return request.app.state.embedder


def get_write_lock(request: Request) -> WriteLock:
    return request.app.state.write_lock


# ---------- MCP SSE 路由 ----------

@app.get("/sse")
async def mcp_sse_endpoint(request: Request):
    """MCP SSE 连接端点"""
    # API Key 认证
    api_key_auth = get_api_key_auth(request)
    await api_key_auth.authenticate(request)

    async with request.app.state.sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await request.app.state.mcp_server.run(
            read_stream,
            write_stream,
            request.app.state.mcp_server.create_initialization_options(),
        )


# ---------- API 端点（供 Agent 直接调用）----------

@app.get("/api/search")
async def api_search(
    request: Request,
    q: str,
    top_k: int = 5,
    path: str = "",
):
    api_key_auth = get_api_key_auth(request)
    await api_key_auth.authenticate(request, required_scope="read")
    tools = get_tools(request)
    result = await tools.search_knowledge(q, top_k, filter_path=path)
    return JSONResponse(result)


@app.post("/api/documents")
async def api_add_document(request: Request):
    api_key_auth = get_api_key_auth(request)
    await api_key_auth.authenticate(request, required_scope="write")
    tools = get_tools(request)
    body = await request.json()
    result = await tools.add_document(
        title=body.get("title", ""),
        content=body.get("content", ""),
        path=body.get("path", ""),
        tags=body.get("tags") or [],
    )
    return JSONResponse(result)


@app.put("/api/documents/{doc_id}")
async def api_update_document(request: Request, doc_id: str):
    api_key_auth = get_api_key_auth(request)
    await api_key_auth.authenticate(request, required_scope="write")
    tools = get_tools(request)
    body = await request.json()
    result = await tools.update_document(
        doc_id=doc_id,
        title=body.get("title", ""),
        content=body.get("content", ""),
        path=body.get("path", ""),
        tags=body.get("tags") or [],
    )
    return JSONResponse(result)


@app.delete("/api/documents/{doc_id}")
async def api_delete_document(request: Request, doc_id: str):
    api_key_auth = get_api_key_auth(request)
    await api_key_auth.authenticate(request, required_scope="write")
    tools = get_tools(request)
    result = await tools.delete_document(doc_id)
    return JSONResponse(result)


@app.get("/api/documents")
async def api_list_documents(
    request: Request,
    path: str = "",
    limit: int = 20,
    offset: int = 0,
):
    api_key_auth = get_api_key_auth(request)
    await api_key_auth.authenticate(request, required_scope="read")
    tools = get_tools(request)
    result = await tools.list_documents(path=path, limit=limit, offset=offset)
    return JSONResponse(result)


@app.get("/api/directories")
async def api_list_directories(request: Request):
    api_key_auth = get_api_key_auth(request)
    await api_key_auth.authenticate(request, required_scope="read")
    tools = get_tools(request)
    result = await tools.list_directories()
    return JSONResponse(result)


# ---------- 健康检查 ----------

@app.get("/health")
async def health_check(request: Request):
    health = {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {},
    }

    # Redis
    try:
        await request.app.state.redis.ping()
        health["services"]["redis"] = "ok"
    except Exception as e:
        health["services"]["redis"] = f"error: {str(e)}"
        health["status"] = "degraded"

    # Chroma
    try:
        request.app.state.chroma.heartbeat()
        health["services"]["chroma"] = "ok"
    except Exception as e:
        health["services"]["chroma"] = f"error: {str(e)}"
        health["status"] = "degraded"

    # Ollama
    try:
        ollama_ok = await request.app.state.embedder.health_check()
        health["services"]["ollama"] = "ok" if ollama_ok else "unavailable"
        if not ollama_ok:
            health["status"] = "degraded"
    except Exception as e:
        health["services"]["ollama"] = f"error: {str(e)}"
        health["status"] = "degraded"

    # MinIO
    try:
        request.app.state.source_store.client.bucket_exists(settings.MINIO_BUCKET)
        health["services"]["minio"] = "ok"
    except Exception as e:
        health["services"]["minio"] = f"error: {str(e)}"
        health["status"] = "degraded"

    status_code = status.HTTP_200_OK if health["status"] == "ok" else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(health, status_code=status_code)


# ---------- 指标端点 ----------

@app.get("/metrics")
async def metrics(request: Request):
    """运行指标（JSON 格式）"""
    uptime = time.time() - START_TIME
    kb = request.app.state.kb
    api_key_auth = request.app.state.api_key_auth

    doc_count = 0
    try:
        doc_count = await kb.count_documents()
    except Exception:
        pass

    key_count = 0
    try:
        keys = await api_key_auth.list_keys()
        key_count = len(keys)
    except Exception:
        pass

    return JSONResponse({
        "app": settings.APP_NAME,
        "version": "1.0.0",
        "uptime_seconds": round(uptime, 2),
        "uptime_human": f"{int(uptime // 86400)}d {int((uptime % 86400) // 3600)}h {int((uptime % 3600) // 60)}m",
        "documents_total": doc_count,
        "api_keys_total": key_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ---------- 导入后台管理路由 ----------

try:
    from admin.routes import router as admin_router
    app.include_router(admin_router, prefix="/admin")
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

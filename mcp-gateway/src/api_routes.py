"""REST API endpoints — /api/*, /health, /metrics."""
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException, status
from fastapi.responses import JSONResponse

from config import get_settings
from models import AddDocumentRequest, UpdateDocumentRequest
from dependencies import get_tools, get_api_key_auth, get_kb

settings = get_settings()

api_router = APIRouter()
START_TIME = time.time()


# ---------- API 端点 ----------

@api_router.get("/api/search")
async def api_search(
    request: Request,
    q: str,
    top_k: int = 5,
    path: str = "",
    tags: str = "",
):
    api_key_auth = get_api_key_auth(request)
    await api_key_auth.authenticate(request, required_scope="read")
    tools = get_tools(request)
    tag_list = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()] if tags else []
    result = await tools.search_knowledge(q, top_k, filter_tags=tag_list, filter_path=path)
    return JSONResponse(result)


@api_router.post("/api/documents")
async def api_add_document(request: Request, body: AddDocumentRequest):
    api_key_auth = get_api_key_auth(request)
    await api_key_auth.authenticate(request, required_scope="write")
    tools = get_tools(request)
    result = await tools.add_document(
        title=body.title, content=body.content,
        path=body.path, tags=body.tags,
    )
    return JSONResponse(result)


@api_router.put("/api/documents/{doc_id}")
async def api_update_document(request: Request, doc_id: str, body: UpdateDocumentRequest):
    api_key_auth = get_api_key_auth(request)
    await api_key_auth.authenticate(request, required_scope="write")
    tools = get_tools(request)
    result = await tools.update_document(
        doc_id=doc_id, title=body.title, content=body.content,
        path=body.path, tags=body.tags,
    )
    return JSONResponse(result)


@api_router.delete("/api/documents/{doc_id}")
async def api_delete_document(request: Request, doc_id: str):
    api_key_auth = get_api_key_auth(request)
    await api_key_auth.authenticate(request, required_scope="write")
    tools = get_tools(request)
    result = await tools.delete_document(doc_id)
    return JSONResponse(result)


@api_router.get("/api/documents")
async def api_list_documents(
    request: Request,
    path: str = "",
    tags: str = "",
    limit: int = 20,
    offset: int = 0,
):
    api_key_auth = get_api_key_auth(request)
    await api_key_auth.authenticate(request, required_scope="read")
    tools = get_tools(request)
    tag_list = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()] if tags else []
    result = await tools.list_documents(tags=tag_list, path=path, limit=limit, offset=offset)
    return JSONResponse(result)


@api_router.get("/api/directories")
async def api_list_directories(request: Request):
    api_key_auth = get_api_key_auth(request)
    await api_key_auth.authenticate(request, required_scope="read")
    tools = get_tools(request)
    result = await tools.list_directories()
    return JSONResponse(result)


# ---------- 健康检查 ----------

@api_router.get("/health")
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
        if hasattr(request.app.state.embedder, "status"):
            health["embedding_providers"] = request.app.state.embedder.status()
        if not ollama_ok:
            health["status"] = "degraded"
    except Exception as e:
        health["services"]["ollama"] = f"error: {str(e)}"
        health["status"] = "degraded"

    # MinIO / LocalFileStore
    try:
        store = request.app.state.source_store
        if hasattr(store, 'client'):
            store.client.bucket_exists(settings.MINIO_BUCKET)
        else:
            store.bucket_exists(settings.MINIO_BUCKET)
        health["services"]["minio"] = "ok"
    except Exception as e:
        health["services"]["minio"] = f"error: {str(e)}"
        health["status"] = "degraded"

    status_code = status.HTTP_200_OK if health["status"] == "ok" else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(health, status_code=status_code)


# ---------- 指标端点 ----------

@api_router.get("/metrics")
async def metrics(request: Request):
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
        "version": settings.APP_VERSION,
        "uptime_seconds": round(uptime, 2),
        "uptime_human": f"{int(uptime // 86400)}d {int((uptime % 86400) // 3600)}h {int((uptime % 3600) // 60)}m",
        "documents_total": doc_count,
        "api_keys_total": key_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

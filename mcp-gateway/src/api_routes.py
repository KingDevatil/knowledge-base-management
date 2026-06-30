"""REST API endpoints — /api/*, /health, /metrics."""
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException, status
from fastapi.responses import JSONResponse

from config import get_settings
from models import AddDocumentRequest, SimilarDocumentsRequest, UpdateDocumentRequest, UpsertDocumentRequest
from dependencies import get_tools, get_api_key_auth, get_kb
from path_permissions import has_path_access
from directory_tree import DirectoryTree

settings = get_settings()

api_router = APIRouter()
START_TIME = time.time()


async def _document_path(request: Request, doc_id: str) -> str:
    kb = get_kb(request)
    doc = await kb._doc_index_get(doc_id)
    return str((doc or {}).get("path", ""))


def _require_path(api_key_info, path: str):
    if not has_path_access(api_key_info, path):
        raise HTTPException(status_code=403, detail="API Key has no access to this path")


def _audit(request: Request, action: str, api_key_info, target_type: str, target_id: str, details: dict, success: bool = True, error: str = ""):
    logger = getattr(get_tools(request), "audit_logger", None)
    if not logger:
        return
    logger.log(
        action=action,
        actor_type="api_key",
        actor=getattr(api_key_info, "key_prefix", "") or getattr(api_key_info, "applicant", "api"),
        target_type=target_type,
        target_id=target_id,
        detail={**details, **({"error": error} if error else {})},
        success=success,
    )


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
    api_key_info = await api_key_auth.authenticate(request, required_scope="read")
    if path:
        _require_path(api_key_info, path)
    tools = get_tools(request)
    tag_list = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()] if tags else []
    result = await tools.search_knowledge(q, top_k, filter_tags=tag_list, filter_path=path)
    if not path and api_key_info.path_mode == "restricted":
        result["results"] = [item for item in result.get("results", []) if has_path_access(api_key_info, item.get("path", ""))]
        result["total"] = len(result["results"])
    return JSONResponse(result)


@api_router.post("/api/documents")
async def api_add_document(request: Request, body: AddDocumentRequest):
    api_key_auth = get_api_key_auth(request)
    api_key_info = await api_key_auth.authenticate(request, required_scope="write")
    _require_path(api_key_info, body.path)
    tools = get_tools(request)
    result = await tools.add_document(
        title=body.title, content=body.content,
        path=body.path, tags=body.tags,
    )
    _audit(request, "api.add_document", api_key_info, "document", str(result.get("doc_id", "")), {"path": body.path, "title": body.title})
    return JSONResponse(result)


@api_router.put("/api/documents/{doc_id}")
async def api_update_document(request: Request, doc_id: str, body: UpdateDocumentRequest):
    api_key_auth = get_api_key_auth(request)
    api_key_info = await api_key_auth.authenticate(request, required_scope="write")
    old_path = await _document_path(request, doc_id)
    _require_path(api_key_info, old_path)
    if body.path:
        _require_path(api_key_info, body.path)
    tools = get_tools(request)
    result = await tools.update_document(
        doc_id=doc_id, title=body.title, content=body.content,
        path=body.path, tags=body.tags,
    )
    _audit(request, "api.update_document", api_key_info, "document", doc_id, {"old_path": old_path, "path": body.path, "title": body.title})
    return JSONResponse(result)


@api_router.delete("/api/documents/{doc_id}")
async def api_delete_document(request: Request, doc_id: str):
    api_key_auth = get_api_key_auth(request)
    api_key_info = await api_key_auth.authenticate(request, required_scope="write")
    _require_path(api_key_info, await _document_path(request, doc_id))
    tools = get_tools(request)
    result = await tools.delete_document(doc_id)
    _audit(request, "api.delete_document", api_key_info, "document", doc_id, {})
    return JSONResponse(result)


@api_router.post("/api/documents/similar")
async def api_find_similar_documents(request: Request, body: SimilarDocumentsRequest):
    api_key_auth = get_api_key_auth(request)
    api_key_info = await api_key_auth.authenticate(request, required_scope="read")
    _require_path(api_key_info, body.path)
    tools = get_tools(request)
    result = await tools.find_similar_documents(
        title=body.title,
        content=body.content,
        path=body.path,
        top_k=body.top_k,
    )
    if api_key_info.path_mode == "restricted":
        result["matches"] = [item for item in result.get("matches", []) if has_path_access(api_key_info, item.get("path", ""))]
        result["total"] = len(result["matches"])
    return JSONResponse(result)


@api_router.post("/api/documents/upsert")
async def api_upsert_document(request: Request, body: UpsertDocumentRequest):
    api_key_auth = get_api_key_auth(request)
    api_key_info = await api_key_auth.authenticate(request, required_scope="write")
    _require_path(api_key_info, body.path)
    tools = get_tools(request)
    result = await tools.upsert_document(
        title=body.title,
        content=body.content,
        path=body.path,
        tags=body.tags,
        match_strategy=body.match_strategy,
        on_conflict=body.on_conflict,
        created_by=body.created_by,
    )
    _audit(request, "api.upsert_document", api_key_info, "document", str(result.get("doc_id", "")), {
        "path": body.path,
        "title": body.title,
        "match_strategy": body.match_strategy,
        "on_conflict": body.on_conflict,
        "action": result.get("action", ""),
    })
    return JSONResponse(result)


@api_router.get("/api/documents/{doc_id}/versions")
async def api_list_document_versions(request: Request, doc_id: str):
    api_key_auth = get_api_key_auth(request)
    api_key_info = await api_key_auth.authenticate(request, required_scope="read")
    _require_path(api_key_info, await _document_path(request, doc_id))
    tools = get_tools(request)
    return JSONResponse(await tools.list_document_versions(doc_id))


@api_router.post("/api/documents/{doc_id}/versions/{version_id}/restore")
async def api_restore_document_version(request: Request, doc_id: str, version_id: str):
    api_key_auth = get_api_key_auth(request)
    api_key_info = await api_key_auth.authenticate(request, required_scope="write")
    _require_path(api_key_info, await _document_path(request, doc_id))
    tools = get_tools(request)
    try:
        version = tools.version_store.get_version(doc_id, version_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document version not found")
    _require_path(api_key_info, str(version.get("path", "")))
    result = await tools.restore_document_version(doc_id, version_id, restored_by="api")
    _audit(request, "api.restore_document_version", api_key_info, "document", doc_id, {"version_id": version_id})
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
    api_key_info = await api_key_auth.authenticate(request, required_scope="read")
    if path:
        _require_path(api_key_info, path)
    tools = get_tools(request)
    tag_list = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()] if tags else []
    result = await tools.list_documents(tags=tag_list, path=path, limit=limit, offset=offset)
    if not path and api_key_info.path_mode == "restricted":
        result["documents"] = [item for item in result.get("documents", []) if has_path_access(api_key_info, item.get("path", ""))]
        result["total"] = len(result["documents"])
    return JSONResponse(result)


@api_router.get("/api/directories")
async def api_list_directories(request: Request):
    api_key_auth = get_api_key_auth(request)
    api_key_info = await api_key_auth.authenticate(request, required_scope="read")
    tools = get_tools(request)
    if api_key_info.path_mode == "restricted":
        docs, _ = await get_kb(request).list_documents(limit=10000, offset=0)
        metadatas = [{"path": d.path} for d in docs if has_path_access(api_key_info, d.path)]
        return JSONResponse({"tree": DirectoryTree.build_from_metadata(metadatas)})
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

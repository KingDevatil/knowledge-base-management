"""Admin page routes (GET - returns HTML or JSON for UI rendering)."""
import markdown
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from directory_tree import DirectoryTree
from directory_store import merge_into_tree

from logger import get_logger

from .helpers import (templates, settings, get_current_user, require_admin,
                      get_current_admin, format_datetime, format_relative_time,
                      check_path_access)  # noqa: F401

logger = get_logger()

from admin_auth import is_admin_role

page_router = APIRouter()


# ---------- 登录/登出 ----------

@page_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/admin/dashboard", error: str = ""):
    return templates.TemplateResponse(request, "login.html", {
        "request": request, "next": next, "error": error,
    })


@page_router.get("/logout")
async def logout(request: Request):
    admin_auth = request.app.state.admin_auth
    try:
        await admin_auth.logout(request)
    except Exception:
        pass
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie("session")
    return response


# ---------- 仪表盘 (admin only) ----------

@page_router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: dict = Depends(require_admin)):
    kb = request.app.state.kb
    api_key_auth = request.app.state.api_key_auth

    doc_count = await kb.count_documents()
    api_keys = await api_key_auth.list_keys()
    active_keys = [k for k in api_keys if k["status"] == "active"]
    expired_soon = [
        k for k in active_keys
        if k.get("expires_at")
        and (datetime.fromisoformat(k["expires_at"].replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds() < 86400
    ]

    # 读取今日检索次数
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_search_count = 0
    redis = getattr(request.app.state, "redis", None)
    if redis:
        try:
            val = await redis.get(f"stats:search:{today}")
            today_search_count = int(val) if val else 0
        except Exception as e:
            logger.warning(f"Failed to read today's search count from Redis: {e}")

    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request, "admin": user,
        "doc_count": doc_count, "active_key_count": len(active_keys),
        "expired_soon_count": len(expired_soon), "total_key_count": len(api_keys),
        "today_search_count": today_search_count,
    })


# ---------- 目录管理 (admin only) ----------

@page_router.get("/directories", response_class=HTMLResponse)
async def directories_page(request: Request, user: dict = Depends(require_admin)):
    return templates.TemplateResponse(request, "directories.html", {
        "request": request, "admin": user,
    })


@page_router.get("/api/directories")
async def api_directories(request: Request, user: dict = Depends(require_admin)):
    tools = request.app.state.tools
    result = await tools.list_directories()
    tree = result.get("tree", result)
    tree = merge_into_tree(tree)
    return JSONResponse(tree)


@page_router.get("/api/documents")
async def api_documents_by_path(
    request: Request, path: str = "", user: dict = Depends(require_admin),
):
    kb = request.app.state.kb
    docs, _ = await kb.list_documents(path=path, limit=1000, offset=0)
    return JSONResponse([
        {"doc_id": d.doc_id, "title": d.title, "path": d.path, "updated_at": d.updated_at}
        for d in docs
    ])


# ---------- 文档管理 (all roles, path-filtered) ----------

@page_router.get("/documents", response_class=HTMLResponse)
async def document_list(
    request: Request, path: str = "", q: str = "", tag: str = "",
    page: int = 1, user: dict = Depends(get_current_user),
):
    kb = request.app.state.kb
    limit = 20
    offset = (page - 1) * limit
    tags = [t.strip() for t in tag.split(",") if t.strip()] if tag else None

    # 获取所有文档（目录树需要全量，搜索过滤在 Python 层完成）
    if is_admin_role(user["role"]):
        all_docs, _ = await kb.list_documents(limit=10000, offset=0)
    else:
        authorized = user.get("authorized_paths", [])
        if not authorized:
            all_docs = []
        else:
            all_docs, _ = await kb.list_documents_by_paths(authorized, limit=10000, offset=0)

    docs = list(all_docs)

    # 路径过滤
    if path:
        if not is_admin_role(user["role"]) and not check_path_access(user, path):
            raise HTTPException(status_code=403, detail="无权访问此目录")
        docs = [d for d in docs if d.path == path or d.path.startswith(path + "/")]

    # 标签过滤
    if tags:
        docs = [d for d in docs if any(t in d.tags for t in tags)]

    # 搜索过滤：按标题、路径、标签匹配
    if q:
        q_lower = q.strip().lower()
        docs = [
            d for d in docs
            if q_lower in d.title.lower()
            or q_lower in d.path.lower()
            or any(q_lower in t.lower() for t in d.tags)
        ]

    total = len(docs)
    docs = docs[offset:offset + limit]

    tree = DirectoryTree.build_from_metadata([{"path": d.path} for d in all_docs])
    tree = merge_into_tree(tree)
    breadcrumbs = DirectoryTree.get_breadcrumbs(path)

    return templates.TemplateResponse(request, "documents.html", {
        "request": request, "admin": user,
        "documents": docs, "tree": tree, "current_path": path,
        "breadcrumbs": breadcrumbs, "q": q, "tag": tag, "page": page,
        "total": total, "limit": limit,
    })


@page_router.get("/documents/upload", response_class=HTMLResponse)
async def upload_page(request: Request, path: str = "", user: dict = Depends(require_admin)):
    return templates.TemplateResponse(request, "upload.html", {
        "request": request, "admin": user, "path": path,
    })


@page_router.get("/documents/{doc_id}", response_class=HTMLResponse)
async def document_view(request: Request, doc_id: str, user: dict = Depends(get_current_user)):
    kb = request.app.state.kb
    source_store = request.app.state.source_store

    chunks = await kb.get_document_chunks(doc_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="文档不存在")

    meta = chunks[0]["metadata"]
    doc_path = meta.get("path", "")

    # 路径权限检查
    if not check_path_access(user, doc_path):
        raise HTTPException(status_code=403, detail="无权访问此文档")

    source_path = meta.get("source_path", "")

    try:
        if source_path:
            content = source_store.get_source_by_full_path(source_path)
        else:
            content = source_store.get_source(doc_id, doc_path)
    except Exception:
        content = "\n\n".join(chunk.get("content", "") for chunk in chunks)

    # 预处理：段落后的无序/有序列表前补空行（Python-Markdown 需要空行才能识别列表）
    content = re.sub(
        r'^(?![ \t]*[-*+]\s)([^#\n>\[].*)\n(?=\d+\.\s+|[-*+]\s+)',
        r'\1\n\n',
        content, flags=re.MULTILINE,
    )
    html_content = markdown.markdown(content, extensions=[
        "extra", "codehilite", "sane_lists", "toc", "admonition",
    ])

    breadcrumbs = DirectoryTree.get_breadcrumbs(doc_path)

    return templates.TemplateResponse(request, "document_view.html", {
        "request": request, "admin": user, "doc_id": doc_id,
        "title": meta.get("title", ""), "path": doc_path,
        "tags": meta.get("tags", "").split(",") if isinstance(meta.get("tags"), str) else meta.get("tags", []),
        "content": content, "html_content": html_content,
        "source_path": source_path, "chunk_count": len(chunks),
        "created_at": meta.get("created_at", ""), "updated_at": meta.get("updated_at", ""),
        "breadcrumbs": breadcrumbs,
    })


@page_router.get("/documents/{doc_id}/edit", response_class=HTMLResponse)
async def document_edit_page(request: Request, doc_id: str, user: dict = Depends(require_admin)):
    kb = request.app.state.kb
    source_store = request.app.state.source_store

    is_new = doc_id == "new"
    if is_new:
        return templates.TemplateResponse(request, "document_edit.html", {
            "request": request, "admin": user, "doc_id": "",
            "title": "", "path": request.query_params.get("path", ""),
            "tags": "", "content": "", "is_new": True,
        })

    chunks = await kb.get_document_chunks(doc_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="文档不存在")

    meta = chunks[0]["metadata"]
    source_path = meta.get("source_path", "")

    try:
        if source_path:
            content = source_store.get_source_by_full_path(source_path)
        else:
            content = source_store.get_source(doc_id, meta.get("path", ""))
    except Exception:
        content = "\n\n".join(chunk.get("content", "") for chunk in chunks)

    return templates.TemplateResponse(request, "document_edit.html", {
        "request": request, "admin": user, "doc_id": doc_id,
        "title": meta.get("title", ""), "tags": meta.get("tags", ""),
        "path": meta.get("path", ""), "content": content, "is_new": False,
    })


# ---------- API Key 管理 (admin only) ----------

@page_router.get("/api-keys", response_class=HTMLResponse)
async def api_keys_list(request: Request, user: dict = Depends(require_admin)):
    api_key_auth = request.app.state.api_key_auth
    keys = await api_key_auth.list_keys()

    status_filter = request.query_params.get("status", "all")
    if status_filter != "all":
        keys = [k for k in keys if k["status"] == status_filter]

    return templates.TemplateResponse(request, "api_keys.html", {
        "request": request, "admin": user, "keys": keys,
        "current_status": status_filter, "format_datetime": format_datetime,
    })


@page_router.get("/api-keys/create", response_class=HTMLResponse)
async def api_key_create_page(request: Request, user: dict = Depends(require_admin)):
    return templates.TemplateResponse(request, "api_key_create.html", {
        "request": request, "admin": user,
    })


# ---------- 系统设置 (admin only) ----------

@page_router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: dict = Depends(require_admin)):
    return templates.TemplateResponse(request, "settings.html", {
        "request": request, "admin": user, "settings": settings,
    })


# ---------- 账户管理 (all roles) ----------

@page_router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, user: dict = Depends(get_current_user)):
    return templates.TemplateResponse(request, "account.html", {
        "request": request, "admin": user,
    })

import markdown
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from directory_tree import DirectoryTree
from directory_store import merge_into_tree

from .helpers import templates, settings, get_current_admin, format_datetime, format_relative_time

page_router = APIRouter()


# ---------- 登录/登出 ----------

@page_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/admin/dashboard", error: str = ""):
    return templates.TemplateResponse(request, "login.html", {
        "request": request,
        "next": next,
        "error": error,
    })


@page_router.get("/logout")
async def logout(request: Request):
    admin_auth = request.app.state.admin_auth
    try:
        await admin_auth.logout(request)
    except Exception:
        # Redis 异常不应阻止登出：继续删除 Cookie 并跳转
        pass
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie("session")
    return response


# ---------- 仪表盘 ----------

@page_router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, admin: dict = Depends(get_current_admin)):
    kb = request.app.state.kb
    api_key_auth = request.app.state.api_key_auth

    # 统计数据
    doc_count = await kb.count_documents()
    api_keys = await api_key_auth.list_keys()
    active_keys = [k for k in api_keys if k["status"] == "active"]
    expired_soon = [
        k for k in active_keys
        if k.get("expires_at")
        and (datetime.fromisoformat(k["expires_at"].replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds() < 86400
    ]

    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "admin": admin,
        "doc_count": doc_count,
        "active_key_count": len(active_keys),
        "expired_soon_count": len(expired_soon),
        "total_key_count": len(api_keys),
    })


# ---------- 目录管理 ----------

@page_router.get("/directories", response_class=HTMLResponse)
async def directories_page(request: Request, admin: dict = Depends(get_current_admin)):
    return templates.TemplateResponse(request, "directories.html", {
        "request": request,
        "admin": admin,
    })


@page_router.get("/api/directories")
async def api_directories(request: Request, admin: dict = Depends(get_current_admin)):
    """返回目录树 JSON（含用户创建的目录）"""
    tools = request.app.state.tools
    result = await tools.list_directories()
    tree = result.get("tree", result)
    # 合并用户手动创建的目录
    tree = merge_into_tree(tree)
    return JSONResponse(tree)


@page_router.get("/api/documents")
async def api_documents_by_path(
    request: Request,
    path: str = "",
    admin: dict = Depends(get_current_admin),
):
    """按路径查询文档列表（供前端目录树使用）"""
    kb = request.app.state.kb
    docs = await kb.list_documents(path=path, limit=1000, offset=0)
    return JSONResponse([
        {
            "doc_id": d.doc_id,
            "title": d.title,
            "path": d.path,
            "updated_at": d.updated_at,
        }
        for d in docs
    ])


# ---------- 文档管理 ----------

@page_router.get("/documents", response_class=HTMLResponse)
async def document_list(
    request: Request,
    path: str = "",
    q: str = "",
    tag: str = "",
    page: int = 1,
    admin: dict = Depends(get_current_admin),
):
    kb = request.app.state.kb
    tools = request.app.state.tools
    limit = 20
    offset = (page - 1) * limit

    tags = [t.strip() for t in tag.split(",") if t.strip()] if tag else None

    # 获取文档列表
    docs = await kb.list_documents(tags=tags, path=path, limit=limit, offset=offset)

    # 获取目录树（合并用户创建的目录）
    all_docs = await kb.list_documents(limit=10000, offset=0)
    tree = DirectoryTree.build_from_metadata([{"path": d.path} for d in all_docs])
    tree = merge_into_tree(tree)

    # 面包屑
    breadcrumbs = DirectoryTree.get_breadcrumbs(path)

    return templates.TemplateResponse(request, "documents.html", {
        "request": request,
        "admin": admin,
        "documents": docs,
        "tree": tree,
        "current_path": path,
        "breadcrumbs": breadcrumbs,
        "q": q,
        "tag": tag,
        "page": page,
    })


@page_router.get("/documents/upload", response_class=HTMLResponse)
async def upload_page(
    request: Request,
    path: str = "",
    admin: dict = Depends(get_current_admin),
):
    return templates.TemplateResponse(request, "upload.html", {
        "request": request,
        "admin": admin,
        "path": path,
    })


@page_router.get("/documents/{doc_id}", response_class=HTMLResponse)
async def document_view(request: Request, doc_id: str, admin: dict = Depends(get_current_admin)):
    kb = request.app.state.kb
    source_store = request.app.state.source_store

    chunks = await kb.get_document_chunks(doc_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="文档不存在")

    meta = chunks[0]["metadata"]
    source_path = meta.get("source_path", "")

    # 从 MinIO 读取原始内容
    try:
        if source_path:
            content = source_store.get_source_by_full_path(source_path)
        else:
            content = source_store.get_source(doc_id, meta.get("path", ""))
    except Exception:
        content = ""

    # Markdown 转 HTML
    html_content = markdown.markdown(content, extensions=[
        "extra",        # 包含 footnotes, abbr, attr_list, def_list, fenced_code, tables, codehilite, sane_lists, smarty, toc
        "codehilite",   # 代码高亮
        "sane_lists",   # 更合理的列表
        "toc",          # 目录
        "admonition",   # 提示块
    ])

    # 面包屑
    doc_path = meta.get("path", "")
    breadcrumbs = DirectoryTree.get_breadcrumbs(doc_path)

    return templates.TemplateResponse(request, "document_view.html", {
        "request": request,
        "admin": admin,
        "doc_id": doc_id,
        "title": meta.get("title", ""),
        "path": doc_path,
        "tags": meta.get("tags", "").split(",") if isinstance(meta.get("tags"), str) else meta.get("tags", []),
        "content": content,
        "html_content": html_content,
        "source_path": source_path,
        "chunk_count": len(chunks),
        "created_at": meta.get("created_at", ""),
        "updated_at": meta.get("updated_at", ""),
        "breadcrumbs": breadcrumbs,
    })


@page_router.get("/documents/{doc_id}/edit", response_class=HTMLResponse)
async def document_edit_page(
    request: Request,
    doc_id: str,
    admin: dict = Depends(get_current_admin),
):
    kb = request.app.state.kb
    source_store = request.app.state.source_store

    is_new = doc_id == "new"
    if is_new:
        return templates.TemplateResponse(request, "document_edit.html", {
            "request": request,
            "admin": admin,
            "doc_id": "",
            "title": "",
            "path": request.query_params.get("path", ""),
            "tags": "",
            "content": "",
            "is_new": True,
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
        content = ""

    tags = meta.get("tags", "")
    if isinstance(tags, list):
        tags = ", ".join(tags)

    return templates.TemplateResponse(request, "document_edit.html", {
        "request": request,
        "admin": admin,
        "doc_id": doc_id,
        "title": meta.get("title", ""),
        "path": meta.get("path", ""),
        "tags": tags,
        "content": content,
        "is_new": False,
    })


# ---------- API Key 管理 ----------

@page_router.get("/api-keys", response_class=HTMLResponse)
async def api_key_list(
    request: Request,
    status: str = "active",
    admin: dict = Depends(get_current_admin),
):
    api_key_auth = request.app.state.api_key_auth
    keys = await api_key_auth.list_keys(status_filter=status if status != "all" else None)

    return templates.TemplateResponse(request, "api_keys.html", {
        "request": request,
        "admin": admin,
        "keys": keys,
        "current_status": status,
    })


@page_router.get("/api-keys/create", response_class=HTMLResponse)
async def api_key_create_page(request: Request, admin: dict = Depends(get_current_admin)):
    return templates.TemplateResponse(request, "api_key_create.html", {
        "request": request,
        "admin": admin,
    })


# ---------- 系统设置 ----------

@page_router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, admin: dict = Depends(get_current_admin)):
    return templates.TemplateResponse(request, "settings.html", {
        "request": request,
        "admin": admin,
        "settings": {
            "ollama_model": settings.OLLAMA_MODEL,
            "ollama_url": settings.OLLAMA_URL,
            "chunk_size": settings.CHUNK_SIZE,
            "chunk_overlap": settings.CHUNK_OVERLAP,
            "write_lock_ttl": settings.WRITE_LOCK_TTL,
            "rate_limit": settings.RATE_LIMIT_DEFAULT,
        },
    })


# ---------- 账户管理 ----------

@page_router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, admin: dict = Depends(get_current_admin)):
    return templates.TemplateResponse(request, "account.html", {
        "request": request,
        "admin": admin,
    })

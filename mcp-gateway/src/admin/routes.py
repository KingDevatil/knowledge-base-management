import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import markdown
from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from config import get_settings
from directory_tree import DirectoryTree
from chunker import chunk_markdown
from lock import WriteLockError

router = APIRouter()

# 模板目录
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)

settings = get_settings()


# ---------- 辅助函数 ----------

async def get_current_admin(request: Request):
    """获取当前登录的管理员"""
    admin_auth = request.app.state.admin_auth
    return await admin_auth.verify_session(request)


def format_datetime(dt_str: str) -> str:
    """格式化日期时间"""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return dt_str


def format_relative_time(dt_str: str) -> str:
    """相对时间"""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = now - dt
        if diff.days > 30:
            return dt.strftime("%Y-%m-%d")
        elif diff.days > 0:
            return f"{diff.days}天前"
        elif diff.seconds > 3600:
            return f"{diff.seconds // 3600}小时前"
        elif diff.seconds > 60:
            return f"{diff.seconds // 60}分钟前"
        else:
            return "刚刚"
    except ValueError:
        return dt_str


# 注册模板过滤器
templates.env.filters["datetime"] = format_datetime
templates.env.filters["relative_time"] = format_relative_time


# ---------- 登录/登出 ----------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/admin/dashboard", error: str = ""):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "next": next,
        "error": error,
    })


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin/dashboard"),
):
    admin_auth = request.app.state.admin_auth
    user = await admin_auth.authenticate(username, password)
    if not user:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "next": next,
            "error": "用户名或密码错误",
        }, status_code=401)

    token = admin_auth.create_session_token(user["username"], user["role"])
    response = RedirectResponse(url=next, status_code=302)
    response.set_cookie(
        key="session",
        value=token,
        max_age=settings.SESSION_MAX_AGE,
        httponly=True,
        secure=not settings.DEBUG,
        samesite="lax",
    )
    return response


@router.get("/logout")
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

@router.get("/dashboard", response_class=HTMLResponse)
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

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "admin": admin,
        "doc_count": doc_count,
        "active_key_count": len(active_keys),
        "expired_soon_count": len(expired_soon),
        "total_key_count": len(api_keys),
    })


# ---------- 目录管理 ----------

@router.get("/directories", response_class=HTMLResponse)
async def directories_page(request: Request, admin: dict = Depends(get_current_admin)):
    return templates.TemplateResponse("directories.html", {
        "request": request,
        "admin": admin,
    })


@router.get("/api/directories")
async def api_directories(request: Request, admin: dict = Depends(get_current_admin)):
    """返回目录树 JSON（含用户创建的目录）"""
    tools = request.app.state.tools
    result = await tools.list_directories()
    tree = result.get("tree", result)
    # 合并用户手动创建的目录
    from directory_store import merge_into_tree
    tree = merge_into_tree(tree)
    return JSONResponse(tree)


@router.post("/api/directories/create")
async def api_create_directory(request: Request, admin: dict = Depends(get_current_admin)):
    """创建新目录"""
    body = await request.json()
    path = body.get("path", "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="目录路径不能为空")
    validated = DirectoryTree.validate_path(path)
    if not validated:
        raise HTTPException(status_code=400, detail="目录路径无效")
    # 持久化保存目录
    from directory_store import create_directory
    if create_directory(validated):
        return JSONResponse({"path": validated, "message": "目录创建成功"})
    raise HTTPException(status_code=500, detail="目录保存失败")


@router.get("/api/documents")
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


@router.post("/api/upload")
async def api_batch_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    path: str = Form(""),
    tags: str = Form(""),
    admin: dict = Depends(get_current_admin),
):
    """批量上传接口（JS 逐文件提交）"""
    tools = request.app.state.tools
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    results = []

    for file in files:
        if not file.filename or not file.filename.endswith(".md"):
            results.append({"filename": file.filename, "status": "skipped", "reason": "仅支持 .md"})
            continue
        try:
            content = (await file.read()).decode("utf-8")
            title = file.filename.rsplit(".", 1)[0]
            result = await tools.import_markdown(
                title=title,
                markdown_content=content,
                path=path,
                tags=tag_list,
                created_by=admin["username"],
            )
            results.append({"filename": file.filename, "status": "ok", "doc_id": result["doc_id"]})
        except UnicodeDecodeError:
            results.append({"filename": file.filename, "status": "error", "reason": "编码错误，请使用 UTF-8"})
        except WriteLockError:
            results.append({"filename": file.filename, "status": "error", "reason": "写锁被占用"})
        except Exception as e:
            results.append({"filename": file.filename, "status": "error", "reason": str(e)})

    return JSONResponse({"results": results})


# ---------- 文档管理 ----------

@router.get("/documents", response_class=HTMLResponse)
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

    # 获取目录树
    all_docs = await kb.list_documents(limit=10000, offset=0)
    tree = DirectoryTree.build_from_metadata([{"path": d.path} for d in all_docs])

    # 面包屑
    breadcrumbs = DirectoryTree.get_breadcrumbs(path)

    return templates.TemplateResponse("documents.html", {
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


@router.get("/documents/upload", response_class=HTMLResponse)
async def upload_page(
    request: Request,
    path: str = "",
    admin: dict = Depends(get_current_admin),
):
    return templates.TemplateResponse("upload.html", {
        "request": request,
        "admin": admin,
        "path": path,
    })


@router.get("/documents/{doc_id}", response_class=HTMLResponse)
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

    return templates.TemplateResponse("document_view.html", {
        "request": request,
        "admin": admin,
        "doc_id": doc_id,
        "title": meta.get("title", ""),
        "path": meta.get("path", ""),
        "tags": meta.get("tags", "").split(",") if isinstance(meta.get("tags"), str) else meta.get("tags", []),
        "content": content,
        "html_content": html_content,
        "source_path": source_path,
        "chunk_count": len(chunks),
        "created_at": meta.get("created_at", ""),
        "updated_at": meta.get("updated_at", ""),
    })


@router.get("/documents/{doc_id}/edit", response_class=HTMLResponse)
async def document_edit_page(
    request: Request,
    doc_id: str,
    admin: dict = Depends(get_current_admin),
):
    kb = request.app.state.kb
    source_store = request.app.state.source_store

    is_new = doc_id == "new"
    if is_new:
        return templates.TemplateResponse("document_edit.html", {
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

    return templates.TemplateResponse("document_edit.html", {
        "request": request,
        "admin": admin,
        "doc_id": doc_id,
        "title": meta.get("title", ""),
        "path": meta.get("path", ""),
        "tags": tags,
        "content": content,
        "is_new": False,
    })


@router.post("/documents/{doc_id}/edit")
async def document_edit_submit(
    request: Request,
    doc_id: str,
    title: str = Form(...),
    content: str = Form(...),
    path: str = Form(""),
    tags: str = Form(""),
    admin: dict = Depends(get_current_admin),
):
    tools = request.app.state.tools
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    try:
        if doc_id == "new" or not doc_id:
            result = await tools.add_document(
                title=title,
                content=content,
                path=path,
                tags=tag_list,
                created_by=admin["username"],
            )
            doc_id = result["doc_id"]
        else:
            await tools.update_document(
                doc_id=doc_id,
                title=title,
                content=content,
                path=path,
                tags=tag_list,
                updated_by=admin["username"],
            )
    except WriteLockError:
        return templates.TemplateResponse("document_edit.html", {
            "request": request,
            "admin": admin,
            "doc_id": doc_id,
            "title": title,
            "path": path,
            "tags": tags,
            "content": content,
            "is_new": doc_id == "new" or not doc_id,
            "error": "写入锁被占用，请稍后重试",
        })

    return RedirectResponse(url=f"/admin/documents/{doc_id}", status_code=302)


@router.post("/documents/{doc_id}/delete")
async def document_delete(
    request: Request,
    doc_id: str,
    admin: dict = Depends(get_current_admin),
):
    tools = request.app.state.tools
    try:
        await tools.delete_document(doc_id, deleted_by=admin["username"])
    except WriteLockError:
        raise HTTPException(status_code=423, detail="写入锁被占用")

    if request.headers.get("HX-Request"):
        return HTMLResponse("")
    return RedirectResponse(url="/admin/documents", status_code=302)


# ---------- 文档上传 ----------

@router.post("/documents/upload")
async def upload_submit(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(""),
    path: str = Form(""),
    tags: str = Form(""),
    admin: dict = Depends(get_current_admin),
):
    # 验证文件类型
    if not file.filename or not file.filename.endswith(".md"):
        return templates.TemplateResponse("upload.html", {
            "request": request,
            "admin": admin,
            "path": path,
            "error": "仅支持 .md 文件",
        })

    # 读取内容
    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return templates.TemplateResponse("upload.html", {
            "request": request,
            "admin": admin,
            "path": path,
            "error": "文件编码错误，请使用 UTF-8 编码",
        })

    # 自动推断标题
    if not title:
        title = file.filename.rsplit(".", 1)[0]

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    tools = request.app.state.tools
    try:
        result = await tools.import_markdown(
            title=title,
            markdown_content=text,
            path=path,
            tags=tag_list,
            created_by=admin["username"],
        )
    except WriteLockError:
        return templates.TemplateResponse("upload.html", {
            "request": request,
            "admin": admin,
            "path": path,
            "error": "写入锁被占用，请稍后重试",
        })

    doc_id = result["doc_id"]
    return RedirectResponse(url=f"/admin/documents/{doc_id}", status_code=302)


# ---------- API Key 管理 ----------

@router.get("/api-keys", response_class=HTMLResponse)
async def api_key_list(
    request: Request,
    status: str = "active",
    admin: dict = Depends(get_current_admin),
):
    api_key_auth = request.app.state.api_key_auth
    keys = await api_key_auth.list_keys(status_filter=status if status != "all" else None)

    return templates.TemplateResponse("api_keys.html", {
        "request": request,
        "admin": admin,
        "keys": keys,
        "current_status": status,
    })


@router.get("/api-keys/create", response_class=HTMLResponse)
async def api_key_create_page(request: Request, admin: dict = Depends(get_current_admin)):
    return templates.TemplateResponse("api_key_create.html", {
        "request": request,
        "admin": admin,
    })


@router.post("/api-keys/create")
async def api_key_create_submit(
    request: Request,
    applicant: str = Form(...),
    applicant_note: str = Form(""),
    scope_read: bool = Form(False),
    scope_write: bool = Form(False),
    duration: str = Form("7d"),
    admin: dict = Depends(get_current_admin),
):
    scope = []
    if scope_read:
        scope.append("read")
    if scope_write:
        scope.append("write")
    if not scope:
        scope = ["read"]

    api_key_auth = request.app.state.api_key_auth
    full_key = await api_key_auth.create_key(
        applicant=applicant,
        applicant_note=applicant_note,
        scope=scope,
        duration=duration,
        created_by=admin["username"],
    )

    return templates.TemplateResponse("api_key_create.html", {
        "request": request,
        "admin": admin,
        "success": True,
        "full_key": full_key,
        "applicant": applicant,
        "scope": scope,
        "duration": duration,
    })


@router.post("/api-keys/{key_hash}/revoke")
async def api_key_revoke(
    request: Request,
    key_hash: str,
    admin: dict = Depends(get_current_admin),
):
    api_key_auth = request.app.state.api_key_auth
    success = await api_key_auth.revoke_key(key_hash, admin["username"])
    if not success:
        raise HTTPException(status_code=404, detail="Key 不存在")

    if request.headers.get("HX-Request"):
        return HTMLResponse("")
    return RedirectResponse(url="/admin/api-keys", status_code=302)


# ---------- 系统设置 ----------

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, admin: dict = Depends(get_current_admin)):
    return templates.TemplateResponse("settings.html", {
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

@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, admin: dict = Depends(get_current_admin)):
    return templates.TemplateResponse("account.html", {
        "request": request,
        "admin": admin,
    })


@router.post("/account/change-password")
async def change_password(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    admin: dict = Depends(get_current_admin),
):
    # 验证两次新密码是否一致
    if new_password != confirm_password:
        return templates.TemplateResponse("account.html", {
            "request": request,
            "admin": admin,
            "error": "两次输入的新密码不一致",
        }, status_code=400)

    # 验证新密码长度
    if len(new_password) < 6:
        return templates.TemplateResponse("account.html", {
            "request": request,
            "admin": admin,
            "error": "新密码长度至少 6 位",
        }, status_code=400)

    admin_auth = request.app.state.admin_auth
    success, message = await admin_auth.change_password(
        username=admin["username"],
        old_password=old_password,
        new_password=new_password,
    )

    if not success:
        return templates.TemplateResponse("account.html", {
            "request": request,
            "admin": admin,
            "error": message,
        }, status_code=400)

    return templates.TemplateResponse("account.html", {
        "request": request,
        "admin": admin,
        "success": True,
        "message": message,
    })

"""Admin API routes (POST/PUT/DELETE - form submissions and JSON APIs)."""
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from directory_tree import DirectoryTree
from chunker import chunk_markdown
from lock import WriteLockError
from models import ReindexByPathRequest

from .helpers import templates, settings, get_current_admin, get_current_user, require_admin
from admin_auth import is_admin_role

api_router = APIRouter()


# ---------- 登录 ----------

def _validate_redirect_url(next_url: str, default: str = "/admin/dashboard") -> str:
    """Validate redirect URL is a safe relative path (prevent Open Redirect)."""
    if not next_url:
        return default
    # Only allow relative paths starting with /
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return default


# ---------- 登录 ----------

LOGIN_RATE_KEY = "login_rate"
LOGIN_RATE_LIMIT = 5        # max attempts per minute
LOGIN_RATE_WINDOW = 60      # seconds
LOGIN_LOCKOUT_LIMIT = 15    # lockout after this many failures
LOGIN_LOCKOUT_WINDOW = 300  # 5-minute lockout


@api_router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin/dashboard"),
):
    # ---- Rate limiting (prevent brute force) ----
    redis = request.app.state.redis
    rate_key = f"{LOGIN_RATE_KEY}:{username}"
    attempts_str = await redis.get(rate_key)
    attempts = int(attempts_str) if attempts_str else 0

    if attempts >= LOGIN_LOCKOUT_LIMIT:
        ttl = await redis.ttl(rate_key)
        wait_min = max(1, (ttl // 60) if ttl > 0 else LOGIN_LOCKOUT_WINDOW // 60)
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "next": next,
            "error": f"登录尝试过多，请 {wait_min} 分钟后再试",
        }, status_code=429)
    elif attempts >= LOGIN_RATE_LIMIT:
        ttl = await redis.ttl(rate_key)
        wait_sec = ttl if ttl > 0 else LOGIN_RATE_WINDOW
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "next": next,
            "error": f"登录过于频繁，请 {wait_sec} 秒后再试",
        }, status_code=429)
    # ---- End rate limiting ----

    admin_auth = request.app.state.admin_auth
    account = await admin_auth.authenticate(username, password)
    if not account:
        # Increment failure counter with sliding window
        if attempts == 0:
            await redis.setex(rate_key, LOGIN_RATE_WINDOW, 1)
        else:
            await redis.incr(rate_key)
            # Extend window on repeated failures
            if attempts >= LOGIN_RATE_LIMIT:
                await redis.expire(rate_key, LOGIN_LOCKOUT_WINDOW)

        return templates.TemplateResponse(request, "login.html", {
            "request": request, "next": next, "error": "用户名或密码错误",
        }, status_code=401)

    # Clear rate limit on success
    await redis.delete(rate_key)

    token = admin_auth.create_session_token(
        account["username"], account["role"], account.get("authorized_paths", []))

    # 普通用户默认跳转到文档管理
    if not is_admin_role(account["role"]) and next == "/admin/dashboard":
        next = "/admin/documents"

    # Validate redirect target (prevent Open Redirect)
    redirect_url = _validate_redirect_url(next)

    response = RedirectResponse(url=redirect_url, status_code=302)
    response.set_cookie(
        key="session", value=token, max_age=settings.SESSION_MAX_AGE,
        httponly=True, secure=not settings.DEBUG, samesite="lax",
    )
    return response


# ---------- 目录管理 (admin only) ----------

@api_router.post("/api/directories/create")
async def api_create_directory(request: Request, user: dict = Depends(require_admin)):
    body = await request.json()
    path = body.get("path", "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="目录路径不能为空")
    validated = DirectoryTree.validate_path(path)
    if not validated:
        raise HTTPException(status_code=400, detail="目录路径无效")
    from directory_store import create_directory
    if create_directory(validated):
        return JSONResponse({"path": validated, "message": "目录创建成功"})
    raise HTTPException(status_code=500, detail="目录保存失败")


# ---------- 文档上传 (admin only) ----------

@api_router.post("/api/upload")
async def api_batch_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    path: str = Form(""),
    tags: str = Form(""),
    user: dict = Depends(require_admin),
):
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
                title=title, markdown_content=content, path=path,
                tags=tag_list, created_by=user["username"],
            )
            results.append({"filename": file.filename, "status": "ok", "doc_id": result["doc_id"]})
        except UnicodeDecodeError:
            results.append({"filename": file.filename, "status": "error", "reason": "编码错误，请使用 UTF-8"})
        except WriteLockError:
            results.append({"filename": file.filename, "status": "error", "reason": "写锁被占用"})
        except Exception as e:
            results.append({"filename": file.filename, "status": "error", "reason": str(e)})

    return JSONResponse({"results": results})


@api_router.post("/documents/upload")
async def upload_submit(
    request: Request,
    files: list[UploadFile] = File(...),
    path: str = Form(""),
    tags: str = Form(""),
    existing_path: str = Form(""),
    user: dict = Depends(require_admin),
):
    tools = request.app.state.tools
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    target_path = path or existing_path
    results = []

    for file in files:
        if not file.filename or not file.filename.endswith(".md"):
            continue
        try:
            content = (await file.read()).decode("utf-8")
            title = file.filename.rsplit(".", 1)[0]
            result = await tools.import_markdown(
                title=title, markdown_content=content, path=target_path,
                tags=tag_list, created_by=user["username"],
            )
            results.append({"filename": file.filename, "doc_id": result["doc_id"]})
        except Exception:
            pass

    if results:
        redirect_path = f"/admin/documents?path={target_path}" if target_path else "/admin/documents"
        return RedirectResponse(url=redirect_path, status_code=302)
    return RedirectResponse(url=f"/admin/documents/upload?path={target_path}", status_code=302)


# ---------- 文档保存 (admin only) ----------

@api_router.post("/documents/{doc_id}/edit")
async def document_save(
    request: Request,
    doc_id: str,
    title: str = Form(...),
    content: str = Form(""),
    path: str = Form(""),
    tags: str = Form(""),
    user: dict = Depends(require_admin),
):
    tools = request.app.state.tools
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    if doc_id and doc_id != "new":
        try:
            await tools.update_document(
                doc_id=doc_id, title=title, content=content,
                path=path, tags=tag_list, updated_by=user["username"],
            )
        except WriteLockError:
            raise HTTPException(status_code=423, detail="写入锁被占用")
    else:
        try:
            result = await tools.add_document(
                title=title, content=content, path=path,
                tags=tag_list, created_by=user["username"],
            )
            doc_id = result.get("doc_id", "")
        except WriteLockError:
            raise HTTPException(status_code=423, detail="写入锁被占用")

    return RedirectResponse(url=f"/admin/documents/{doc_id}", status_code=302)


# ---------- 文档删除 (admin only) ----------

@api_router.post("/documents/{doc_id}/delete")
async def document_delete(
    request: Request, doc_id: str, user: dict = Depends(require_admin),
):
    tools = request.app.state.tools
    try:
        await tools.delete_document(doc_id, deleted_by=user["username"])
    except WriteLockError:
        raise HTTPException(status_code=423, detail="写入锁被占用")

    if request.headers.get("HX-Request"):
        return HTMLResponse("")
    return RedirectResponse(url="/admin/documents", status_code=302)


# ---------- 文档重索引 (admin only) ----------

@api_router.post("/documents/{doc_id}/reindex")
async def document_reindex(
    request: Request, doc_id: str, user: dict = Depends(require_admin),
):
    tools = request.app.state.tools
    try:
        result = await tools.reindex_document(doc_id)
    except WriteLockError:
        raise HTTPException(status_code=423, detail="写入锁被占用")

    if request.headers.get("HX-Request"):
        return RedirectResponse(
            url=request.headers.get("HX-Current-URL", f"/admin/documents/{doc_id}"),
            status_code=302,
        )
    return RedirectResponse(url=f"/admin/documents/{doc_id}", status_code=302)


@api_router.post("/api/reindex-by-path")
async def api_reindex_by_path(
    request: Request, body: ReindexByPathRequest, user: dict = Depends(require_admin),
):
    path = body.path
    kb = request.app.state.kb
    tools = request.app.state.tools

    docs = await kb.list_documents(path=path, limit=10000, offset=0)
    results = []
    for doc in docs:
        try:
            r = await tools.reindex_document(doc.doc_id)
            results.append({"doc_id": doc.doc_id, "title": doc.title, "status": "ok", "chunks_new": r["chunks_new"]})
        except HTTPException as e:
            results.append({"doc_id": doc.doc_id, "title": doc.title, "status": "error", "detail": e.detail})
        except Exception as e:
            results.append({"doc_id": doc.doc_id, "title": doc.title, "status": "error", "detail": str(e)})

    success_count = sum(1 for r in results if r["status"] == "ok")
    error_count = len(results) - success_count

    if request.headers.get("HX-Request"):
        error_details = "".join(
            f'<p class="text-xs text-red-500 mt-1">- {r["title"]}: {r["detail"]}</p>'
            for r in results if r["status"] == "error"
        )
        return HTMLResponse(f'''
<div id="reindex-all-container">
    <div class="px-4 py-3 rounded-xl text-sm font-medium
        {"bg-emerald-50 dark:bg-emerald-500/10 border border-emerald-200 dark:border-emerald-500/20 text-emerald-700 dark:text-emerald-400" if error_count == 0 else "bg-amber-50 dark:bg-amber-500/10 border border-amber-200 dark:border-amber-500/20 text-amber-700 dark:text-amber-400"}">
        <div class="flex items-center gap-2">
            <i data-lucide="{"check-circle" if error_count == 0 else "alert-triangle"}" class="w-4 h-4"></i>
            <span>重新切片完成：{success_count} 个成功，{error_count} 个失败</span>
            <button onclick="this.parentElement.parentElement.remove()" class="ml-auto text-slate-400 hover:text-slate-600">&times;</button>
        </div>
        {error_details}
    </div>
</div>
<script>if(typeof lucide!=='undefined')lucide.createIcons()</script>
''')

    return JSONResponse({
        "success": True, "total": len(docs),
        "success_count": success_count, "results": results,
    })


# ---------- API Key 管理 (admin only) ----------

@api_router.post("/api-keys/create")
async def api_key_create(
    request: Request,
    applicant: str = Form(...),
    applicant_note: str = Form(""),
    scope_read: bool = Form(False),
    scope_write: bool = Form(False),
    duration: str = Form("30"),
    user: dict = Depends(require_admin),
):
    if not scope_read and not scope_write:
        scope_read = True

    scope = []
    if scope_read:
        scope.append("read")
    if scope_write:
        scope.append("write")

    try:
        duration_days = int(duration) if duration != "forever" else None
    except ValueError:
        duration_days = None

    api_key_auth = request.app.state.api_key_auth
    api_key = await api_key_auth.create_key(
        applicant=applicant,
        applicant_note=applicant_note,
        scope=scope,
        duration=duration_days,
        created_by=user["username"],
    )

    return templates.TemplateResponse(request, "api_key_create.html", {
        "request": request, "admin": user, "created_key": api_key, "applicant": applicant,
    })


@api_router.post("/api-keys/{key_prefix}/revoke")
async def api_key_revoke(
    request: Request, key_prefix: str, user: dict = Depends(require_admin),
):
    api_key_auth = request.app.state.api_key_auth
    success = await api_key_auth.revoke_key(key_prefix)
    if not success:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    return JSONResponse({"success": True, "message": "API Key 已吊销"})


# ---------- 账户管理 (all roles) ----------

@api_router.post("/account/change-password")
async def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: dict = Depends(get_current_user),
):
    if new_password != confirm_password:
        return templates.TemplateResponse(request, "account.html", {
            "request": request, "admin": user, "error": "两次输入的新密码不一致",
        })

    if len(new_password) < 6:
        return templates.TemplateResponse(request, "account.html", {
            "request": request, "admin": user, "error": "密码长度至少 6 位",
        })

    admin_auth = request.app.state.admin_auth
    success, msg = await admin_auth.change_password(user["username"], current_password, new_password)
    if not success:
        return templates.TemplateResponse(request, "account.html", {
            "request": request, "admin": user, "error": msg,
        })

    return templates.TemplateResponse(request, "account.html", {
        "request": request, "admin": user, "success": "密码修改成功",
    })


# ---------- 分享管理 (all roles, path-checked) ----------

SHARE_TOKEN_BYTES = 24
SHARE_PREFIX = "share"


def _share_key(token: str) -> str:
    return f"{SHARE_PREFIX}:{token}"


@api_router.post("/api/documents/{doc_id}/share/create")
async def share_create(
    request: Request, doc_id: str,
    user: dict = Depends(get_current_user),
):
    body = await request.json()
    duration_days = body.get("duration_days", 7)

    from .helpers import check_path_access
    kb = request.app.state.kb
    chunks = await kb.get_document_chunks(doc_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="文档不存在")

    doc_path = chunks[0]["metadata"].get("path", "")
    if not check_path_access(user, doc_path):
        raise HTTPException(status_code=403, detail="无权分享此文档")

    token = secrets.token_hex(SHARE_TOKEN_BYTES)
    expires = datetime.now(timezone.utc) + timedelta(days=duration_days)

    share_data = {
        "doc_id": doc_id,
        "title": chunks[0]["metadata"].get("title", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires.isoformat(),
        "created_by": user["username"],
    }

    redis = request.app.state.redis
    await redis.hset(_share_key(token), mapping=share_data)
    await redis.expireat(_share_key(token), expires)

    return JSONResponse({
        "token": token,
        "url": f"{request.base_url}share/{token}",
        "expires_at": expires.isoformat(),
    })


@api_router.post("/api/documents/{doc_id}/share/revoke")
async def share_revoke(
    request: Request, doc_id: str,
    user: dict = Depends(get_current_user),
):
    body = await request.json()
    token = body.get("token", "").strip()
    if not token or not re.match(r'^[a-f0-9]+$', token, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="无效的 token")

    key = _share_key(token)
    redis = request.app.state.redis
    existing = await redis.hget(key, "doc_id")
    owner_bytes = await redis.hget(key, "created_by")
    existing_doc_id = existing.decode() if isinstance(existing, bytes) else existing
    creator = owner_bytes.decode() if isinstance(owner_bytes, bytes) else (owner_bytes or "")

    if not existing_doc_id or existing_doc_id != doc_id:
        raise HTTPException(status_code=404, detail="分享链接不存在")

    # 管理员可撤销所有，普通用户仅可撤销自己创建的
    if not is_admin_role(user["role"]) and creator != user["username"]:
        raise HTTPException(status_code=403, detail="只能撤销自己创建的分享链接")

    await redis.delete(key)
    return JSONResponse({"success": True})


@api_router.get("/api/documents/{doc_id}/shares")
async def share_list(
    request: Request, doc_id: str,
    user: dict = Depends(get_current_user),
):
    redis = request.app.state.redis
    shares = []
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=f"{SHARE_PREFIX}:*", count=100)
        for key in keys:
            data = await redis.hgetall(key)
            kw = k_raw = key
            if isinstance(k_raw, bytes):
                kw = k_raw.decode()
            raw_doc_id = data.get(b"doc_id", data.get("doc_id", ""))
            raw_doc_id = raw_doc_id.decode() if isinstance(raw_doc_id, bytes) else raw_doc_id
            if raw_doc_id != doc_id:
                continue

            raw_creator = data.get(b"created_by", data.get("created_by", ""))
            raw_creator = raw_creator.decode() if isinstance(raw_creator, bytes) else raw_creator

            # 普通用户只能看到自己创建的分享
            if not is_admin_role(user["role"]) and raw_creator != user["username"]:
                continue

            tk = kw.split(":", 1)[1]
            shares.append({
                "token": tk[:8] + "..." + tk[-4:],
                "full_token": tk,
                "title": (data.get(b"title", data.get("title", b"")).decode()
                          if isinstance(data.get(b"title", data.get("title", b"")), bytes)
                          else data.get("title", "")),
                "created_at": (data.get(b"created_at", data.get("created_at", b"")).decode()
                               if isinstance(data.get(b"created_at", data.get("created_at", b"")), bytes)
                               else data.get("created_at", "")),
                "expires_at": (data.get(b"expires_at", data.get("expires_at", b"")).decode()
                               if isinstance(data.get(b"expires_at", data.get("expires_at", b"")), bytes)
                               else data.get("expires_at", "")),
                "created_by": raw_creator,
            })
        if cursor == 0 or len(shares) >= 50:
            break

    shares.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return JSONResponse(shares)

"""Admin API routes (POST/PUT/DELETE - form submissions and JSON APIs)."""
import json
import uuid

from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from directory_tree import DirectoryTree
from chunker import chunk_markdown
from lock import WriteLockError

from .helpers import templates, settings, get_current_admin

api_router = APIRouter()


# ---------- 登录 ----------

@api_router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin/dashboard"),
):
    admin_auth = request.app.state.admin_auth
    user = await admin_auth.authenticate(username, password)
    if not user:
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "next": next, "error": "用户名或密码错误",
        }, status_code=401)

    token = admin_auth.create_session_token(user["username"], user["role"])
    response = RedirectResponse(url=next, status_code=302)
    response.set_cookie(
        key="session", value=token, max_age=settings.SESSION_MAX_AGE,
        httponly=True, secure=not settings.DEBUG, samesite="lax",
    )
    return response


# ---------- 目录管理 ----------

@api_router.post("/api/directories/create")
async def api_create_directory(request: Request, admin: dict = Depends(get_current_admin)):
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


# ---------- 文档上传 ----------

@api_router.post("/api/upload")
async def api_batch_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    path: str = Form(""),
    tags: str = Form(""),
    admin: dict = Depends(get_current_admin),
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
                tags=tag_list, created_by=admin["username"],
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
    admin: dict = Depends(get_current_admin),
):
    """上传页面提交（HTML 表单）"""
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
                tags=tag_list, created_by=admin["username"],
            )
            results.append({"filename": file.filename, "doc_id": result["doc_id"]})
        except Exception:
            pass

    if results:
        redirect_path = f"/admin/documents?path={target_path}" if target_path else "/admin/documents"
        return RedirectResponse(url=redirect_path, status_code=302)
    return RedirectResponse(url=f"/admin/documents/upload?path={target_path}", status_code=302)


# ---------- 文档保存 ----------

@api_router.post("/documents/{doc_id}/edit")
async def document_save(
    request: Request,
    doc_id: str,
    title: str = Form(...),
    content: str = Form(""),
    path: str = Form(""),
    tags: str = Form(""),
    admin: dict = Depends(get_current_admin),
):
    tools = request.app.state.tools
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    if doc_id and doc_id != "new":
        try:
            await tools.update_document(
                doc_id=doc_id, title=title, content=content,
                path=path, tags=tag_list, updated_by=admin["username"],
            )
        except WriteLockError:
            raise HTTPException(status_code=423, detail="写入锁被占用")
    else:
        try:
            result = await tools.add_document(
                title=title, content=content, path=path,
                tags=tag_list, created_by=admin["username"],
            )
            doc_id = result.get("doc_id", "")
        except WriteLockError:
            raise HTTPException(status_code=423, detail="写入锁被占用")

    return RedirectResponse(url=f"/admin/documents/{doc_id}", status_code=302)


# ---------- 文档删除 ----------

@api_router.post("/documents/{doc_id}/delete")
async def document_delete(
    request: Request, doc_id: str, admin: dict = Depends(get_current_admin),
):
    tools = request.app.state.tools
    try:
        await tools.delete_document(doc_id, deleted_by=admin["username"])
    except WriteLockError:
        raise HTTPException(status_code=423, detail="写入锁被占用")

    if request.headers.get("HX-Request"):
        return HTMLResponse("")
    return RedirectResponse(url="/admin/documents", status_code=302)


# ---------- 文档重索引 ----------

@api_router.post("/documents/{doc_id}/reindex")
async def document_reindex(
    request: Request, doc_id: str, admin: dict = Depends(get_current_admin),
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
    request: Request, admin: dict = Depends(get_current_admin),
):
    body = await request.json()
    path = body.get("path", "")
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


# ---------- API Key 管理 ----------

@api_router.post("/api-keys/create")
async def api_key_create(
    request: Request,
    applicant: str = Form(...),
    applicant_note: str = Form(""),
    scope_read: bool = Form(False),
    scope_write: bool = Form(False),
    duration: str = Form("30"),
    admin: dict = Depends(get_current_admin),
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
        created_by=admin["username"],
    )

    return templates.TemplateResponse(request, "api_key_create.html", {
        "request": request, "admin": admin,
        "created_key": api_key, "applicant": applicant,
    })


@api_router.post("/api-keys/{key_prefix}/revoke")
async def api_key_revoke(
    request: Request, key_prefix: str, admin: dict = Depends(get_current_admin),
):
    api_key_auth = request.app.state.api_key_auth
    success = await api_key_auth.revoke_key(key_prefix)
    if not success:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    return JSONResponse({"success": True, "message": "API Key 已吊销"})


# ---------- 账户管理 ----------

@api_router.post("/account/change-password")
async def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    admin: dict = Depends(get_current_admin),
):
    if new_password != confirm_password:
        return templates.TemplateResponse(request, "account.html", {
            "request": request, "admin": admin,
            "error": "两次输入的新密码不一致",
        })

    if len(new_password) < 6:
        return templates.TemplateResponse(request, "account.html", {
            "request": request, "admin": admin,
            "error": "密码长度至少 6 位",
        })

    admin_auth = request.app.state.admin_auth
    success = await admin_auth.change_password(admin["username"], current_password, new_password)
    if not success:
        return templates.TemplateResponse(request, "account.html", {
            "request": request, "admin": admin,
            "error": "当前密码错误",
        })

    return templates.TemplateResponse(request, "account.html", {
        "request": request, "admin": admin,
        "success": "密码修改成功",
    })

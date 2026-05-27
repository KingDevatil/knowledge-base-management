"""Admin miscellaneous API routes — directories, API Keys, backup export."""
import io
import os
import re
import zipfile
from datetime import datetime

from pydantic import BaseModel
from fastapi import APIRouter, Request, Form, HTTPException, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, StreamingResponse

from directory_tree import DirectoryTree
from .helpers import templates, require_admin
from admin_auth import is_admin_role

admin_misc_router = APIRouter()


# ---------- 目录管理 (admin only) ----------

class RenameDirRequest(BaseModel):
    old_path: str
    new_path: str


class DeleteDirRequest(BaseModel):
    path: str


@admin_misc_router.post("/api/directories/create")
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


@admin_misc_router.post("/api/directories/rename")
async def api_dir_rename(request: Request, body: RenameDirRequest, user: dict = Depends(require_admin)):
    tools = request.app.state.tools
    result = await tools.rename_directory(body.old_path, body.new_path)
    return JSONResponse(result)


@admin_misc_router.post("/api/directories/delete")
async def api_dir_delete(request: Request, body: DeleteDirRequest, user: dict = Depends(require_admin)):
    tools = request.app.state.tools
    result = await tools.delete_directory(body.path)
    return JSONResponse(result)


# ---------- API Key 管理 (admin only) ----------

@admin_misc_router.post("/api-keys/create")
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

    if duration in ("forever", "permanent"):
        dur_value = "permanent"
    else:
        try:
            days = int(duration)
            dur_value = f"{days}d"
        except ValueError:
            dur_value = "7d"

    api_key_auth = request.app.state.api_key_auth
    api_key = await api_key_auth.create_key(
        applicant=applicant,
        applicant_note=applicant_note,
        scope=scope,
        duration=dur_value,
        created_by=user["username"],
    )

    return templates.TemplateResponse(request, "api_key_create.html", {
        "request": request, "admin": user, "created_key": api_key, "applicant": applicant,
        "success": True,
        "duration": "长期有效" if dur_value == "permanent" else
                    f"{dur_value[:-1]} 天" if dur_value.endswith("d") else
                    str(dur_value),
    })


@admin_misc_router.post("/api-keys/{key_prefix}/revoke")
async def api_key_revoke(
    request: Request, key_prefix: str, user: dict = Depends(require_admin),
):
    api_key_auth = request.app.state.api_key_auth
    revoked_by = user.get("username", "admin") if user else "admin"

    target = await api_key_auth.find_key(key_prefix)
    if not target:
        raise HTTPException(status_code=404, detail="API Key 不存在")

    success = await api_key_auth.revoke_key(target["key_hash"], revoked_by)
    if not success:
        raise HTTPException(status_code=404, detail="API Key 不存在")

    status_param = request.query_params.get("status", "")
    redirect_url = f"/admin/api-keys?status={status_param}" if status_param else "/admin/api-keys"
    return RedirectResponse(url=redirect_url, status_code=302)


@admin_misc_router.post("/api-keys/{key_prefix}/delete")
async def api_key_delete(
    request: Request, key_prefix: str, user: dict = Depends(require_admin),
):
    api_key_auth = request.app.state.api_key_auth

    target = await api_key_auth.find_key(key_prefix)
    if not target:
        raise HTTPException(status_code=404, detail="API Key 不存在")

    if target.get("status") != "revoked":
        raise HTTPException(status_code=400, detail="只能删除已吊销的 API Key")

    success = await api_key_auth.delete_key(target["key_hash"])
    if not success:
        raise HTTPException(status_code=404, detail="API Key 不存在")

    status_param = request.query_params.get("status", "")
    redirect_url = f"/admin/api-keys?status={status_param}" if status_param else "/admin/api-keys"
    return RedirectResponse(url=redirect_url, status_code=302)


# ---------- 备份导出 (admin only) ----------

def _sanitize_filename(name: str) -> str:
    """去除文件名中的非法字符，保留可读性"""
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', name)
    return sanitized.strip(' ._') or 'untitled'


@admin_misc_router.get("/api/backup/export")
async def api_backup_export(
    request: Request,
    user: dict = Depends(require_admin),
):
    """导出知识库所有文档为 .md 文件，按目录结构打包为 ZIP 下载"""
    kb = request.app.state.kb
    tools = request.app.state.tools

    listing = await kb.list_documents(limit=10000, offset=0)
    docs = listing.get("documents", [])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in docs:
            doc_id = doc.get("doc_id")
            if not doc_id:
                continue
            try:
                detail = await tools.get_document(doc_id)
            except Exception:
                continue

            title = detail.get("title", "untitled")
            content = detail.get("content", "")
            doc_path = detail.get("path", "")

            safe_name = _sanitize_filename(title)
            if doc_path:
                arcname = f"{doc_path.replace('/', os.sep)}/{safe_name}.md"
            else:
                arcname = f"{safe_name}.md"

            zf.writestr(arcname, content)

    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=kb-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip",
            "Content-Type": "application/zip",
        },
    )

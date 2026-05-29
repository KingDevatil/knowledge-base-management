"""Admin document management API routes — upload, CRUD, reindex."""
import os
import tempfile
import zipfile
import tarfile
import shutil
from urllib.parse import quote

from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse

from lock import WriteLockError
from models import ReindexByPathRequest
from .helpers import require_admin, require_editor, get_current_user, check_path_access

documents_router = APIRouter()


# ---------- 文档上传 (admin only) ----------

@documents_router.post("/api/upload")
async def api_batch_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    path: str = Form(""),
    tags: str = Form(""),
    user: dict = Depends(require_admin),
):
    tools = request.app.state.tools
    tag_list = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
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


# ---------- 压缩包上传 (admin only) ----------

@documents_router.post("/api/upload-archive")
async def api_upload_archive(
    request: Request,
    file: UploadFile = File(...),
    path: str = Form(""),
    tags: str = Form(""),
    user: dict = Depends(require_admin),
):
    """上传压缩包（.zip/.tar.gz），保留目录结构，合并入已有目录"""
    if not file.filename:
        return JSONResponse({"error": "未选择文件"}, status_code=400)

    ext = file.filename.lower()
    if not (ext.endswith('.zip') or ext.endswith('.tar.gz') or ext.endswith('.tgz')):
        return JSONResponse({"error": "仅支持 .zip / .tar.gz"}, status_code=400)

    tmpdir = tempfile.mkdtemp(prefix="kb_archive_")
    archive_path = os.path.join(tmpdir, file.filename)

    try:
        content = await file.read()
        with open(archive_path, 'wb') as f:
            f.write(content)

        extract_dir = os.path.join(tmpdir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)

        if ext.endswith('.zip'):
            with zipfile.ZipFile(archive_path, 'r') as zf:
                zf.extractall(extract_dir)
        else:
            with tarfile.open(archive_path, 'r:gz') as tf:
                tf.extractall(extract_dir)

        md_files = []
        for root, dirs, files_ in os.walk(extract_dir):
            for fn in files_:
                if fn.endswith('.md'):
                    full_path = os.path.join(root, fn)
                    rel_path = os.path.relpath(os.path.dirname(full_path), extract_dir).replace("\\", "/")
                    if rel_path == ".":
                        rel_path = ""
                    md_files.append((full_path, rel_path, fn))

        if not md_files:
            return JSONResponse({"error": "压缩包内没有 .md 文件"}, status_code=400)

        tools = request.app.state.tools
        tag_list = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
        results = []
        success = 0

        for full_path, rel_path, fn in md_files:
            try:
                content_md = open(full_path, 'r', encoding='utf-8').read()
            except UnicodeDecodeError:
                try:
                    content_md = open(full_path, 'r', encoding='gbk').read()
                except Exception:
                    results.append({"filename": fn, "path": rel_path, "status": "error", "reason": "编码错误"})
                    continue

            title = fn.rsplit(".", 1)[0]
            if path and rel_path:
                merged_path = f"{path}/{rel_path}"
            elif path:
                merged_path = path
            else:
                merged_path = rel_path

            try:
                result = await tools.import_markdown(
                    title=title, markdown_content=content_md,
                    path=merged_path, tags=tag_list,
                    created_by=user["username"],
                )
                results.append({"filename": fn, "path": merged_path, "status": "ok", "doc_id": result["doc_id"]})
                success += 1
            except WriteLockError:
                results.append({"filename": fn, "status": "error", "reason": "写锁被占用"})
            except Exception as e:
                results.append({"filename": fn, "status": "error", "reason": str(e)})

        return JSONResponse({
            "results": results,
            "total": len(md_files),
            "success": success,
            "failed": len(md_files) - success,
        })

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 压缩包预览 (admin only) ----------

@documents_router.post("/api/preview-archive")
async def api_preview_archive(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(require_admin),
):
    """预览压缩包内容：返回文件列表但不导入"""
    if not file.filename:
        return JSONResponse({"error": "未选择文件"}, status_code=400)

    ext = file.filename.lower()
    if not (ext.endswith('.zip') or ext.endswith('.tar.gz') or ext.endswith('.tgz')):
        return JSONResponse({"error": "仅支持 .zip / .tar.gz"}, status_code=400)

    MAX_SIZE = 200 * 1024 * 1024
    content = await file.read()
    if len(content) > MAX_SIZE:
        return JSONResponse(
            {"error": f"压缩包过大（{len(content) // 1024 // 1024}MB），最大支持 200MB"},
            status_code=400,
        )

    tmpdir = tempfile.mkdtemp(prefix="kb_preview_")
    archive_path = os.path.join(tmpdir, file.filename)

    try:
        with open(archive_path, 'wb') as f:
            f.write(content)

        extract_dir = os.path.join(tmpdir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)

        if ext.endswith('.zip'):
            with zipfile.ZipFile(archive_path, 'r') as zf:
                zf.extractall(extract_dir)
        else:
            with tarfile.open(archive_path, 'r:gz') as tf:
                tf.extractall(extract_dir)

        files = []
        for root, dirs, fnames in os.walk(extract_dir):
            for fn in fnames:
                if fn.endswith('.md'):
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(os.path.dirname(full), extract_dir).replace("\\", "/")
                    if rel == ".":
                        rel = ""
                    size = os.path.getsize(full)
                    files.append({"filename": fn, "path": rel, "size": size})

        return JSONResponse({"files": files, "total": len(files)})

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@documents_router.post("/documents/upload")
async def upload_submit(
    request: Request,
    files: list[UploadFile] = File(...),
    path: str = Form(""),
    tags: str = Form(""),
    existing_path: str = Form(""),
    user: dict = Depends(require_admin),
):
    tools = request.app.state.tools
    tag_list = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
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

@documents_router.post("/documents/{doc_id}/edit")
async def document_save(
    request: Request,
    doc_id: str,
    title: str = Form(...),
    content: str = Form(""),
    path: str = Form(""),
    tags: str = Form(""),
    user: dict = Depends(require_editor),
):
    tools = request.app.state.tools
    tag_list = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]

    if doc_id and doc_id != "new":
        # 检查权限：user 角色只能编辑授权路径下的文档
        kb = request.app.state.kb
        chunks = await kb.get_document_chunks(doc_id)
        if chunks:
            doc_path = chunks[0]["metadata"].get("path", "")
            if not check_path_access(user, doc_path):
                raise HTTPException(status_code=403, detail="无权编辑此文档")
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

    if path:
        return RedirectResponse(url=f"/admin/documents?path={path}", status_code=302)
    return RedirectResponse(url=f"/admin/documents/{doc_id}", status_code=302)


# ---------- 文档删除 (admin only) ----------

@documents_router.post("/documents/{doc_id}/delete")
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

    form = await request.form()
    redirect_path = form.get("path", "")
    if redirect_path:
        return RedirectResponse(url=f"/admin/documents?path={redirect_path}", status_code=302)
    return RedirectResponse(url="/admin/documents", status_code=302)


# ---------- 文档重索引 (admin only) ----------

@documents_router.post("/documents/{doc_id}/reindex")
async def document_reindex(
    request: Request, doc_id: str, user: dict = Depends(require_editor),
):
    tools = request.app.state.tools
    # user 角色只能重索引授权路径下的文档
    kb = request.app.state.kb
    chunks = await kb.get_document_chunks(doc_id)
    if chunks:
        doc_path = chunks[0]["metadata"].get("path", "")
        if not check_path_access(user, doc_path):
            raise HTTPException(status_code=403, detail="无权重索引此文档")
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


@documents_router.post("/api/reindex-by-path")
async def api_reindex_by_path(
    request: Request, body: ReindexByPathRequest, user: dict = Depends(require_admin),
):
    path = body.path
    kb = request.app.state.kb
    tools = request.app.state.tools

    docs, _ = await kb.list_documents(path=path, limit=10000, offset=0)
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


# ---------- 文档下载 (all authenticated users, path-filtered) ----------

@documents_router.get("/documents/{doc_id}/download")
async def document_download(
    request: Request, doc_id: str, user: dict = Depends(get_current_user),
):
    """下载文档为 .md 文件。"""
    import traceback
    try:
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

        title = meta.get("title", doc_id)
        filename = f"{title}.md"

        return StreamingResponse(
            iter([content]),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        from logger import get_logger
        get_logger().exception(f"Document download failed for {doc_id}")
        return JSONResponse(
            status_code=500,
            content={"detail": f"下载失败: {type(e).__name__}: {e}"},
        )

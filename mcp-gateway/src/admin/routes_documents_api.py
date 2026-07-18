"""Admin document management API routes — upload, CRUD, reindex."""
import os
import tempfile
import shutil
from urllib.parse import quote

from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse

from document_conversion import (
    DocumentConversionError,
    convert_uploaded_document,
    is_supported_document_filename,
)
from document_metadata import (
    extract_document_header_metadata,
    merge_metadata_values,
    normalize_metadata_values,
)
from lock import WriteLockError
from models import MoveDocumentRequest, ReindexByPathRequest, UpdateDocumentMetadataRequest
from .helpers import require_admin, require_editor, get_current_user, check_path_access
from .archive_security import (
    ArchiveValidationError,
    safe_extract_archive,
    validate_archive_size,
)

documents_router = APIRouter()


def _upload_basename(filename: str) -> str:
    return filename.replace("\\", "/").rsplit("/", 1)[-1]


def _upload_title(filename: str) -> str:
    return os.path.splitext(_upload_basename(filename))[0] or "未命名文档"


def _uploaded_metadata(content: str, manual_tags: list[str]) -> dict:
    """Return the metadata that ingestion extracts for the upload-result editor."""

    header = extract_document_header_metadata(content)
    return {
        "tags": merge_metadata_values(manual_tags, header.tags),
        "entities": header.entities,
        "extracted_tags": header.tags,
        "extracted_entities": header.entities,
    }


# ---------- 入库任务 (admin only) ----------

@documents_router.get("/api/ingestion-tasks")
async def api_ingestion_tasks(
    request: Request,
    limit: int = 50,
    user: dict = Depends(require_admin),
):
    tools = request.app.state.tools
    tasks = list(getattr(tools, "ingestion_tasks", {}).values())
    tasks.sort(key=lambda item: item.get("started_at", ""), reverse=True)
    limit = max(1, min(limit, 200))
    return JSONResponse({
        "tasks": tasks[:limit],
        "total": len(tasks),
        "limit": limit,
    })


@documents_router.post("/api/ingestion-tasks/{task_id}/retry")
async def api_retry_ingestion_task(
    request: Request,
    task_id: str,
    user: dict = Depends(require_admin),
):
    tools = request.app.state.tools
    result = await tools.retry_ingestion_task(task_id, retried_by=user["username"])
    if "application/json" not in request.headers.get("accept", ""):
        return RedirectResponse(url="/admin/maintenance", status_code=302)
    return JSONResponse(result)


@documents_router.get("/api/cleanup-tasks")
async def api_cleanup_tasks(
    request: Request,
    limit: int = 50,
    user: dict = Depends(require_admin),
):
    tools = request.app.state.tools
    tasks = list(getattr(tools, "cleanup_tasks", {}).values())
    tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    limit = max(1, min(limit, 200))
    return JSONResponse({
        "tasks": tasks[:limit],
        "total": len(tasks),
        "limit": limit,
    })


@documents_router.post("/api/cleanup-tasks/{task_id}/retry")
async def api_retry_cleanup_task(
    request: Request,
    task_id: str,
    user: dict = Depends(require_admin),
):
    tools = request.app.state.tools
    result = tools.retry_cleanup_task(task_id)
    if "application/json" not in request.headers.get("accept", ""):
        return RedirectResponse(url="/admin/maintenance", status_code=302)
    return JSONResponse(result)


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
    tag_list = normalize_metadata_values(tags)
    results = []

    for file in files:
        if not is_supported_document_filename(file.filename):
            results.append({"filename": file.filename, "status": "skipped", "reason": "仅支持 .md、.csv"})
            continue
        try:
            content = convert_uploaded_document(file.filename, await file.read())
            title = _upload_title(file.filename)
            result = await tools.import_markdown(
                title=title, markdown_content=content, path=path,
                tags=tag_list, created_by=user["username"],
            )
            results.append({
                "filename": file.filename,
                "status": "ok",
                "doc_id": result["doc_id"],
                "task_id": result.get("task_id", ""),
                **_uploaded_metadata(content, tag_list),
            })
        except DocumentConversionError as e:
            results.append({"filename": file.filename, "status": "error", "reason": str(e)})
        except WriteLockError:
            results.append({"filename": file.filename, "status": "error", "reason": "写锁被占用"})
        except Exception as e:
            results.append({"filename": file.filename, "status": "error", "reason": str(e)})

    return JSONResponse({
        "results": results,
        "tasks": [item["task_id"] for item in results if item.get("task_id")],
    })


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
    archive_name = _upload_basename(file.filename)
    archive_path = os.path.join(tmpdir, archive_name)

    try:
        content = await file.read()
        try:
            validate_archive_size(content)
        except ArchiveValidationError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        with open(archive_path, 'wb') as f:
            f.write(content)

        extract_dir = os.path.join(tmpdir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)

        try:
            safe_extract_archive(archive_path, extract_dir, archive_name)
        except ArchiveValidationError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        document_files = []
        for root, dirs, files_ in os.walk(extract_dir):
            for fn in files_:
                if is_supported_document_filename(fn):
                    full_path = os.path.join(root, fn)
                    rel_path = os.path.relpath(os.path.dirname(full_path), extract_dir).replace("\\", "/")
                    if rel_path == ".":
                        rel_path = ""
                    document_files.append((full_path, rel_path, fn))

        if not document_files:
            return JSONResponse({"error": "压缩包内没有 .md 或 .csv 文件"}, status_code=400)

        tools = request.app.state.tools
        tag_list = normalize_metadata_values(tags)
        results = []
        success = 0

        for full_path, rel_path, fn in document_files:
            try:
                with open(full_path, "rb") as document_file:
                    content_md = convert_uploaded_document(fn, document_file.read())
            except DocumentConversionError as e:
                results.append({"filename": fn, "path": rel_path, "status": "error", "reason": str(e)})
                continue

            title = _upload_title(fn)
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
                results.append({
                    "filename": fn,
                    "path": merged_path,
                    "status": "ok",
                    "doc_id": result["doc_id"],
                    "task_id": result.get("task_id", ""),
                    **_uploaded_metadata(content_md, tag_list),
                })
                success += 1
            except WriteLockError:
                results.append({"filename": fn, "status": "error", "reason": "写锁被占用"})
            except Exception as e:
                results.append({"filename": fn, "status": "error", "reason": str(e)})

        return JSONResponse({
            "results": results,
            "tasks": [item["task_id"] for item in results if item.get("task_id")],
            "total": len(document_files),
            "success": success,
            "failed": len(document_files) - success,
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

    content = await file.read()
    try:
        validate_archive_size(content)
    except ArchiveValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    tmpdir = tempfile.mkdtemp(prefix="kb_preview_")
    archive_name = _upload_basename(file.filename)
    archive_path = os.path.join(tmpdir, archive_name)

    try:
        with open(archive_path, 'wb') as f:
            f.write(content)

        extract_dir = os.path.join(tmpdir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)

        try:
            safe_extract_archive(archive_path, extract_dir, archive_name)
        except ArchiveValidationError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        files = []
        for root, dirs, fnames in os.walk(extract_dir):
            for fn in fnames:
                if is_supported_document_filename(fn):
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
    tag_list = normalize_metadata_values(tags)
    target_path = path or existing_path
    results = []

    for file in files:
        if not is_supported_document_filename(file.filename):
            continue
        try:
            content = convert_uploaded_document(file.filename, await file.read())
            title = _upload_title(file.filename)
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
            save_result = await tools.update_document(
                doc_id=doc_id, title=title, content=content,
                path=path, tags=tag_list, updated_by=user["username"],
                path_explicit=True,
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
            save_result = result
        except WriteLockError:
            raise HTTPException(status_code=423, detail="写入锁被占用")

    save_status = "moved" if save_result.get("path_only") else "saved"
    if path:
        return RedirectResponse(
            url=f"/admin/documents?path={quote(path)}&save_status={save_status}",
            status_code=302,
        )
    return RedirectResponse(url=f"/admin/documents?save_status={save_status}", status_code=302)


# ---------- 文档标签 / 实体（正文外独立元数据） ----------

@documents_router.post("/api/documents/{doc_id}/metadata")
async def api_update_document_metadata(
    request: Request,
    doc_id: str,
    body: UpdateDocumentMetadataRequest,
    user: dict = Depends(require_editor),
):
    """Update tags/entities without rewriting the Markdown source document."""

    kb = request.app.state.kb
    doc = await kb._doc_index_get(doc_id)
    if doc:
        doc_path = doc.get("path", "")
    else:
        chunks = await kb.get_document_chunks(doc_id)
        if not chunks:
            raise HTTPException(status_code=404, detail="文档不存在")
        doc_path = chunks[0].get("metadata", {}).get("path", "")

    if not check_path_access(user, doc_path):
        raise HTTPException(status_code=403, detail="无权编辑此文档元数据")

    result = await request.app.state.tools.update_document_metadata(
        doc_id=doc_id,
        tags=normalize_metadata_values(body.tags),
        entities=normalize_metadata_values(body.entities),
        updated_by=user["username"],
    )
    return JSONResponse(result)


@documents_router.post("/api/documents/{doc_id}/move")
async def api_move_document(
    request: Request,
    doc_id: str,
    body: MoveDocumentRequest,
    user: dict = Depends(require_editor),
):
    """Move one document using the path-only update fast path."""

    kb = request.app.state.kb
    current = await kb._doc_index_get(doc_id)
    if current:
        current_path = current.get("path", "")
    else:
        chunks = await kb.get_document_chunks(doc_id)
        if not chunks:
            raise HTTPException(status_code=404, detail="文档不存在")
        current_path = chunks[0].get("metadata", {}).get("path", "")

    if not check_path_access(user, current_path):
        raise HTTPException(status_code=403, detail="无权移动此文档")
    if not check_path_access(user, body.path):
        raise HTTPException(status_code=403, detail="无权移动到目标目录")

    tools = request.app.state.tools
    document = await tools.get_document(doc_id)
    try:
        result = await tools.update_document(
            doc_id=doc_id,
            title=document.get("title", ""),
            content=document.get("content", ""),
            path=body.path,
            tags=document.get("tags", []),
            updated_by=user["username"],
            path_explicit=True,
        )
    except WriteLockError:
        raise HTTPException(status_code=423, detail="写入锁被占用，请稍后重试")
    return JSONResponse(result)


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


@documents_router.get("/api/documents/{doc_id}/versions")
async def api_document_versions(
    request: Request, doc_id: str, user: dict = Depends(require_editor),
):
    kb = request.app.state.kb
    doc = await kb._doc_index_get(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not check_path_access(user, doc.get("path", "")):
        raise HTTPException(status_code=403, detail="No access to this document")
    return JSONResponse(await request.app.state.tools.list_document_versions(doc_id))


@documents_router.post("/api/documents/{doc_id}/versions/{version_id}/restore")
async def api_document_version_restore(
    request: Request, doc_id: str, version_id: str, user: dict = Depends(require_editor),
):
    kb = request.app.state.kb
    doc = await kb._doc_index_get(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not check_path_access(user, doc.get("path", "")):
        raise HTTPException(status_code=403, detail="No access to this document")
    try:
        version = request.app.state.tools.version_store.get_version(doc_id, version_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document version not found")
    if not check_path_access(user, version.get("path", "")):
        raise HTTPException(status_code=403, detail="No access to this document version")
    try:
        result = await request.app.state.tools.restore_document_version(
            doc_id=doc_id,
            version_id=version_id,
            restored_by=user["username"],
        )
    except WriteLockError:
        raise HTTPException(status_code=423, detail="Write lock is busy")
    return JSONResponse(result)


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

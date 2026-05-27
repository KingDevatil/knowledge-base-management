"""
Share link routes — public (no admin auth required).
Token-based access to shared documents.
"""
import re
import markdown
from typing import Dict

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse

from directory_tree import DirectoryTree
from .helpers import templates

share_router = APIRouter()


async def _validate_share_token(request: Request, token: str) -> Dict:
    """Verify share token exists and not expired. Returns share data dict."""
    token = token.strip()
    if not re.match(r'^[a-f0-9]{48}$', token, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="无效的分享链接")
    if len(token) != 48:
        raise HTTPException(status_code=400, detail="无效的分享链接")

    redis = request.app.state.redis
    key = f"share:{token}"
    data = await redis.hgetall(key)

    if not data:
        raise HTTPException(status_code=404, detail="分享链接不存在或已过期")

    # Decode bytes
    result = {}
    for k, v in data.items():
        ks = k.decode() if isinstance(k, bytes) else k
        vs = v.decode() if isinstance(v, bytes) else v
        result[ks] = vs

    return result


@share_router.get("/share/{token}", response_class=HTMLResponse)
async def share_view(request: Request, token: str):
    """公开分享页面 —— 基于 token 鉴权，无需登录"""
    share_data = await _validate_share_token(request, token)
    doc_id = share_data.get("doc_id", "")

    kb = request.app.state.kb
    source_store = request.app.state.source_store

    chunks = await kb.get_document_chunks(doc_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="文档不存在")

    meta = chunks[0]["metadata"]
    source_path = meta.get("source_path", "")

    # 读取源文档（非切片）
    try:
        if source_path:
            content = source_store.get_source_by_full_path(source_path)
        else:
            content = source_store.get_source(doc_id, meta.get("path", ""))
    except Exception:
        content = "\n\n".join(chunk.get("content", "") for chunk in chunks)

    # 预处理：段落后的列表前补空行（同 document_view）
    content = re.sub(
        r'^(?![ \t]*[-*+]\s)([^#\n>\[].*)\n(?=\d+\.\s+|[-*+]\s+)',
        r'\1\n\n',
        content, flags=re.MULTILINE,
    )
    html_content = markdown.markdown(content, extensions=[
        "extra", "codehilite", "sane_lists", "toc", "admonition",
    ]) if content else ""

    return templates.TemplateResponse(request, "share_view.html", {
        "request": request,
        "doc_id": doc_id,
        "title": share_data.get("title", meta.get("title", "")),
        "path": meta.get("path", ""),
        "html_content": html_content,
        "chunk_count": len(chunks),
        "created_at": meta.get("created_at", ""),
        "updated_at": meta.get("updated_at", ""),
        "share_token": token,
        "share_created_at": share_data.get("created_at", ""),
        "share_expires_at": share_data.get("expires_at", ""),
    })

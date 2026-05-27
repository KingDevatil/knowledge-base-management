"""Admin share management API routes."""
import re
import secrets
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse

from .helpers import get_current_user, check_path_access
from admin_auth import is_admin_role

shares_router = APIRouter()

SHARE_TOKEN_BYTES = 24
SHARE_PREFIX = "share"


def _share_key(token: str) -> str:
    return f"{SHARE_PREFIX}:{token}"


@shares_router.post("/api/documents/{doc_id}/share/create")
async def share_create(
    request: Request, doc_id: str,
    user: dict = Depends(get_current_user),
):
    body = await request.json()
    duration_days = body.get("duration_days", 7)

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


@shares_router.post("/api/documents/{doc_id}/share/revoke")
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


@shares_router.get("/api/documents/{doc_id}/shares")
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

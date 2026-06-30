"""Admin page routes (GET - returns HTML or JSON for UI rendering)."""
import json
import markdown
import re
import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from directory_tree import DirectoryTree
from directory_store import merge_into_tree

from logger import get_logger

from .helpers import (templates, settings, get_current_user, require_admin,
                      require_editor, get_current_admin, format_datetime,
                      format_relative_time, check_path_access)  # noqa: F401

logger = get_logger()

from admin_auth import is_admin_role
from kb_graph import KnowledgeGraphBuilder
from consistency import KnowledgeBaseConsistencyChecker
from markdown_security import sanitize_markdown_html
from ddns import (
    DDNS_LEGACY_KEY,
    delete_service as ddns_delete_service,
    list_services as ddns_list_services,
    public_service as ddns_public_service,
    save_service as ddns_save_service,
    test_service as ddns_test_service,
    update_service as ddns_update_service,
)
from env_manager import (
    activate_profile,
    activate_reverse_proxy_config,
    apply_reverse_proxy_config,
    delete_profile,
    delete_reverse_proxy_config,
    get_reverse_proxy_service_state,
    list_profiles,
    list_reverse_proxy_configs,
    is_docker_deployment,
    restart_current_service,
    save_profile,
    save_reverse_proxy_config,
    set_reverse_proxy_service_enabled,
    read_env,
)

page_router = APIRouter()

DDNS_CONFIG_KEY = DDNS_LEGACY_KEY


def _management_password_hash(request: Request, username: str | None) -> str:
    if not username:
        return ""
    try:
        admin_auth = request.app.state.admin_auth
        accounts = admin_auth._load_accounts()
        account = accounts.get(username, {})
        return account.get("management_password_hash", "")
    except Exception as exc:
        logger.warning("Failed to read management password state for settings page: %s", exc)
        return ""


def _has_management_password(request: Request, username: str | None) -> bool:
    return bool(_management_password_hash(request, username))


def _verify_management_password(request: Request, username: str, password: str) -> bool:
    password_hash = _management_password_hash(request, username)
    if not password_hash:
        return False
    return request.app.state.admin_auth.verify_password(password, password_hash)


def _set_management_password(request: Request, username: str, password: str) -> tuple[bool, str]:
    if not password or len(password) < 8:
        return False, "管理密码至少需要 8 个字符。"
    admin_auth = request.app.state.admin_auth
    accounts = admin_auth._load_accounts()
    account = accounts.get(username)
    if not account:
        return False, "当前管理员账号不存在，无法设置管理密码。"
    account["management_password_hash"] = admin_auth.hash_password(password)
    return (True, "管理密码已保存。") if admin_auth._save_accounts(accounts) else (False, "管理密码保存失败，请稍后重试。")


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


@page_router.get("/api/search-trend")
async def api_search_trend(request: Request, user: dict = Depends(require_admin)):
    """返回最近 24 小时的检索趋势数据（用于仪表盘图表）。"""
    redis = getattr(request.app.state, "redis", None)
    if not redis:
        return JSONResponse({"hours": [], "counts": []})

    now = datetime.now(timezone.utc)
    hours = []
    counts = []
    local_offset = timedelta(hours=8)  # UTC → 北京时间
    for i in range(23, -1, -1):
        dt = now - timedelta(hours=i)
        key = f"stats:search:hourly:{dt.strftime('%Y-%m-%d:%H')}"
        val = await redis.get(key)
        # 标签转为北京时间
        local_dt = dt + local_offset
        hours.append(local_dt.strftime('%H:00'))
        counts.append(int(val) if val else 0)

    return JSONResponse({"hours": hours, "counts": counts})


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
    tags = [t.strip() for t in tag.replace("，", ",").split(",") if t.strip()] if tag else None

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
    available_tags = sorted({
        item.strip()
        for d in all_docs
        for item in (
            d.tags
            if isinstance(d.tags, list)
            else str(d.tags or "").replace("，", ",").split(",")
        )
        if item and item.strip()
    })

    return templates.TemplateResponse(request, "documents.html", {
        "request": request, "admin": user,
        "documents": docs, "tree": tree, "directories": tree,
        "current_path": path, "tags": available_tags,
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
    md = markdown.Markdown(extensions=[
        "extra", "codehilite", "sane_lists", "toc", "admonition",
    ], extension_configs={
        "toc": {"toc_depth": "2-4"},
    })
    html_content = sanitize_markdown_html(md.convert(content))
    toc_html = sanitize_markdown_html(md.toc)
    md.reset()

    breadcrumbs = DirectoryTree.get_breadcrumbs(doc_path)

    return templates.TemplateResponse(request, "document_view.html", {
        "request": request, "admin": user, "doc_id": doc_id,
        "title": meta.get("title", ""), "path": doc_path,
        "tags": meta.get("tags", "").replace("，", ",").split(",") if isinstance(meta.get("tags"), str) else meta.get("tags", []),
        "content": content, "html_content": html_content,
        "toc_html": toc_html,
        "source_path": source_path, "chunk_count": len(chunks),
        "created_at": meta.get("created_at", ""), "updated_at": meta.get("updated_at", ""),
        "breadcrumbs": breadcrumbs,
    })


@page_router.get("/documents/{doc_id}/edit", response_class=HTMLResponse)
async def document_edit_page(request: Request, doc_id: str, user: dict = Depends(require_editor)):
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
    doc_path = meta.get("path", "")
    if not check_path_access(user, doc_path):
        raise HTTPException(status_code=403, detail="无权访问此文档")

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
    graph_semantic_threshold = 0.0
    ddns_services = []
    env_profiles = []
    active_env_profile_id = ""
    reverse_proxy_configs = []
    active_reverse_proxy_config_id = ""
    reverse_proxy_service_state = {
        "enabled": False,
        "active_id": "",
        "active_config": None,
        "runtime_host": "",
        "runtime_port": "",
        "docker_deployment": is_docker_deployment(),
    }
    try:
        env_profiles, active_env_profile_id = list_profiles()
    except Exception as exc:
        logger.warning("Failed to load environment profiles for settings page: %s", exc)
    try:
        reverse_proxy_configs, active_reverse_proxy_config_id = list_reverse_proxy_configs()
        reverse_proxy_service_state = get_reverse_proxy_service_state()
    except Exception as exc:
        logger.warning("Failed to load reverse proxy settings for settings page: %s", exc)
    try:
        env_values = read_env()
    except Exception as exc:
        logger.warning("Failed to load environment values for settings page: %s", exc)
        env_values = {}
    kbdata_dir_display = env_values.get("KBDATA_DIR") or settings.KBDATA_DIR or "默认数据目录"
    redis = getattr(request.app.state, "redis", None)
    if redis:
        try:
            val = await redis.get("kb:config:graph:semantic_threshold")
            if val is not None:
                graph_semantic_threshold = float(val)
        except Exception:
            pass
        try:
            ddns_services = [ddns_public_service(item) for item in await ddns_list_services(redis)]
        except Exception:
            pass
    return templates.TemplateResponse(request, "settings.html", {
        "request": request, "admin": user, "settings": settings,
        "graph_semantic_threshold": graph_semantic_threshold,
        "ddns_services": ddns_services,
        "env_profiles": env_profiles,
        "active_env_profile_id": active_env_profile_id,
        "reverse_proxy_configs": reverse_proxy_configs,
        "active_reverse_proxy_config_id": active_reverse_proxy_config_id,
        "reverse_proxy_service_state": reverse_proxy_service_state,
        "kbdata_dir_display": kbdata_dir_display,
        "has_management_password": _has_management_password(request, user.get("username")),
    })


@page_router.get("/maintenance", response_class=HTMLResponse)
async def maintenance_page(request: Request, user: dict = Depends(require_admin)):
    tools = request.app.state.tools
    checker = KnowledgeBaseConsistencyChecker(
        request.app.state.kb,
        request.app.state.source_store,
    )
    try:
        consistency = await checker.check()
    except Exception as e:
        consistency = {
            "success": False,
            "issue_count": 1,
            "issues": [{
                "code": "diagnostic_failed",
                "severity": "error",
                "message": str(e),
                "doc_id": "",
                "details": {},
            }],
            "stats": {"errors": 1, "warnings": 0},
        }

    ingestion_tasks = list(getattr(tools, "ingestion_tasks", {}).values())
    ingestion_tasks.sort(key=lambda item: item.get("started_at", ""), reverse=True)
    cleanup_tasks = list(getattr(tools, "cleanup_tasks", {}).values())
    cleanup_tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)

    return templates.TemplateResponse(request, "maintenance.html", {
        "request": request,
        "admin": user,
        "consistency": consistency,
        "ingestion_tasks": ingestion_tasks[:50],
        "cleanup_tasks": cleanup_tasks[:50],
    })


@page_router.post("/api/save-graph-settings")
async def save_graph_settings(request: Request, user: dict = Depends(require_admin)):
    """保存图谱设置到 Redis"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "无效的 JSON"}, status_code=400)

    redis = getattr(request.app.state, "redis", None)
    if not redis:
        return JSONResponse({"success": False, "error": "Redis 不可用"}, status_code=503)

    threshold = body.get("semantic_threshold")
    if threshold is not None:
        try:
            threshold = float(threshold)
        except (ValueError, TypeError):
            return JSONResponse({"success": False, "error": "无效的阈值，请输入数字"}, status_code=400)
        threshold = max(0.0, min(1.0, threshold))
        try:
            await redis.set("kb:config:graph:semantic_threshold", str(threshold))
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    return JSONResponse({"success": True, "semantic_threshold": threshold})


@page_router.post("/api/save-ddns-settings")
async def save_ddns_settings(request: Request, user: dict = Depends(require_admin)):
    """Save one DDNS service to Redis for the admin console."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "无效的 JSON"}, status_code=400)

    redis = getattr(request.app.state, "redis", None)
    if not redis:
        return JSONResponse({"success": False, "error": "Redis 不可用"}, status_code=503)

    try:
        service = await ddns_save_service(redis, body)
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    return JSONResponse({"success": True, "ddns": ddns_public_service(service)})


@page_router.post("/api/ddns-services/{service_id}/delete")
async def delete_ddns_service(request: Request, service_id: str, user: dict = Depends(require_admin)):
    redis = getattr(request.app.state, "redis", None)
    if not redis:
        return JSONResponse({"success": False, "error": "Redis 不可用"}, status_code=503)
    deleted = await ddns_delete_service(redis, service_id)
    if not deleted:
        return JSONResponse({"success": False, "error": "DDNS 配置不存在"}, status_code=404)
    return JSONResponse({"success": True})


@page_router.post("/api/ddns-services/{service_id}/test")
async def test_ddns_service(request: Request, service_id: str, user: dict = Depends(require_admin)):
    redis = getattr(request.app.state, "redis", None)
    if not redis:
        return JSONResponse({"success": False, "error": "Redis 不可用"}, status_code=503)
    services = await ddns_list_services(redis)
    service = next((item for item in services if item.get("id") == service_id), None)
    if not service:
        return JSONResponse({"success": False, "error": "DDNS 配置不存在"}, status_code=404)
    result = await ddns_test_service(redis, service)
    return JSONResponse(result)


@page_router.post("/api/ddns-services/{service_id}/update")
async def update_ddns_service(request: Request, service_id: str, user: dict = Depends(require_admin)):
    redis = getattr(request.app.state, "redis", None)
    if not redis:
        return JSONResponse({"success": False, "error": "Redis 不可用"}, status_code=503)
    services = await ddns_list_services(redis)
    service = next((item for item in services if item.get("id") == service_id), None)
    if not service:
        return JSONResponse({"success": False, "error": "DDNS 配置不存在"}, status_code=404)
    try:
        result = await ddns_update_service(redis, service)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    return JSONResponse(result)


@page_router.post("/api/env-profiles")
async def save_env_profile(request: Request, user: dict = Depends(require_admin)):
    try:
        body = await request.json()
        profile = save_profile(body)
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    profiles, active_id = list_profiles()
    return JSONResponse({"success": True, "profile": profile, "profiles": profiles, "active_id": active_id})


@page_router.post("/api/env-profiles/{profile_id}/activate")
async def activate_env_profile(request: Request, profile_id: str, user: dict = Depends(require_admin)):
    try:
        profile = activate_profile(profile_id)
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    profiles, active_id = list_profiles()
    return JSONResponse({"success": True, "profile": profile, "profiles": profiles, "active_id": active_id})


@page_router.post("/api/env-profiles/{profile_id}/delete")
async def delete_env_profile(request: Request, profile_id: str, user: dict = Depends(require_admin)):
    try:
        deleted = delete_profile(profile_id)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    if not deleted:
        return JSONResponse({"success": False, "error": "部署模式配置不存在或已被删除。"}, status_code=404)
    profiles, active_id = list_profiles()
    return JSONResponse({"success": True, "profiles": profiles, "active_id": active_id})


@page_router.post("/api/reverse-proxy-configs")
async def save_reverse_proxy(request: Request, user: dict = Depends(require_admin)):
    try:
        body = await request.json()
        config = save_reverse_proxy_config(body)
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    configs, active_id = list_reverse_proxy_configs()
    return JSONResponse({
        "success": True,
        "config": config,
        "configs": configs,
        "active_id": active_id,
        "service_state": get_reverse_proxy_service_state(),
    })


@page_router.post("/api/reverse-proxy-configs/apply")
async def apply_reverse_proxy(request: Request, user: dict = Depends(require_admin)):
    try:
        body = await request.json()
        result = apply_reverse_proxy_config(body)
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    configs, active_id = list_reverse_proxy_configs()
    return JSONResponse({
        "success": True,
        "config": result["config"],
        "configs": configs,
        "active_id": active_id,
        "service_state": result["state"],
    })


@page_router.post("/api/reverse-proxy-service")
async def toggle_reverse_proxy_service(request: Request, user: dict = Depends(require_admin)):
    try:
        body = await request.json()
        state = set_reverse_proxy_service_enabled(bool(body.get("enabled")), body.get("config_id"))
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    configs, active_id = list_reverse_proxy_configs()
    return JSONResponse({
        "success": True,
        "configs": configs,
        "active_id": active_id,
        "service_state": state,
    })


@page_router.post("/api/reverse-proxy-configs/{config_id}/activate")
async def activate_reverse_proxy(request: Request, config_id: str, user: dict = Depends(require_admin)):
    try:
        config = activate_reverse_proxy_config(config_id)
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    configs, active_id = list_reverse_proxy_configs()
    return JSONResponse({
        "success": True,
        "config": config,
        "configs": configs,
        "active_id": active_id,
        "service_state": get_reverse_proxy_service_state(),
    })


@page_router.post("/api/reverse-proxy-configs/{config_id}/delete")
async def delete_reverse_proxy(request: Request, config_id: str, user: dict = Depends(require_admin)):
    try:
        deleted = delete_reverse_proxy_config(config_id)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    if not deleted:
        return JSONResponse({"success": False, "error": "反向代理配置不存在或已被删除。"}, status_code=404)
    configs, active_id = list_reverse_proxy_configs()
    return JSONResponse({
        "success": True,
        "configs": configs,
        "active_id": active_id,
        "service_state": get_reverse_proxy_service_state(),
    })


@page_router.post("/api/management-password")
async def set_management_password(request: Request, user: dict = Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "请求数据格式无效。"}, status_code=400)
    password = str(body.get("password", ""))
    confirm = str(body.get("confirm", ""))
    if password != confirm:
        return JSONResponse({"success": False, "error": "两次输入的管理密码不一致。"}, status_code=400)
    ok, msg = _set_management_password(request, user["username"], password)
    return JSONResponse({"success": ok, "error": "" if ok else msg}, status_code=200 if ok else 400)


@page_router.post("/api/restart-service")
async def restart_service(request: Request, user: dict = Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "请求数据格式无效。"}, status_code=400)
    if not _has_management_password(request, user["username"]):
        return JSONResponse({"success": False, "needs_management_password": True, "error": "当前管理员账号尚未设置管理密码，请先设置后再重启服务。"}, status_code=403)
    if not _verify_management_password(request, user["username"], str(body.get("management_password", ""))):
        return JSONResponse({"success": False, "error": "管理密码错误，请重新输入。"}, status_code=403)
    if is_docker_deployment():
        return JSONResponse({
            "success": False,
            "error": "Docker 部署下不能从容器内部直接重启服务，请在宿主机执行 docker compose restart mcp-gateway。",
        }, status_code=400)
    asyncio.create_task(restart_current_service())
    return JSONResponse({"success": True, "message": "服务重启任务已提交，请稍后刷新页面。"})


# ---------- 知识图谱 (all logged-in users) ----------

@page_router.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request, user: dict = Depends(get_current_user)):
    """知识图谱可视化页面"""
    kb = request.app.state.kb
    embedder = getattr(request.app.state, "embedder", None)
    builder = KnowledgeGraphBuilder(kb, embedder)

    graph_exists = builder.graph_json_path.exists()
    graph_data = None
    graph_stats = None

    if graph_exists:
        try:
            raw = json.loads(builder.graph_json_path.read_text(encoding="utf-8"))
            nodes = raw.get("nodes", [])
            links = raw.get("links", raw.get("edges", []))
            graph_stats = {"nodes": len(nodes), "edges": len(links)}

            # 合并社区标签
            community_labels = {}
            if builder.labels_path.exists():
                try:
                    community_labels = json.loads(builder.labels_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            for n in nodes:
                cid = str(n.get("community", ""))
                if cid in community_labels:
                    n["community_label"] = community_labels[cid]
                else:
                    n["community_label"] = f"分组 {cid}"

            graph_data = raw
        except Exception:
            graph_exists = False

    # 从 Redis 读取阈值
    semantic_threshold = 0.0
    redis = getattr(request.app.state, "redis", None)
    if redis:
        try:
            val = await redis.get("kb:config:graph:semantic_threshold")
            if val is not None:
                semantic_threshold = float(val)
        except Exception:
            pass

    return templates.TemplateResponse(request, "graph.html", {
        "request": request, "admin": user,
        "graph_exists": graph_exists,
        "graph_stats": graph_stats,
        "graph_data": graph_data,
        "semantic_threshold": semantic_threshold,
    })


@page_router.post("/api/rebuild-graph")
async def api_rebuild_graph(request: Request, user: dict = Depends(require_editor)):
    """手动触发图谱重建（读取 Redis 中的阈值设置）"""
    kb = request.app.state.kb
    embedder = getattr(request.app.state, "embedder", None)
    builder = KnowledgeGraphBuilder(kb, embedder)

    # 从 Redis 读取阈值，支持 Query 参数覆盖
    threshold = request.query_params.get("semantic_threshold")
    if threshold is None:
        redis = getattr(request.app.state, "redis", None)
        if redis:
            try:
                val = await redis.get("kb:config:graph:semantic_threshold")
                if val is not None:
                    threshold = float(val)
            except Exception:
                pass
    threshold = float(threshold) if threshold else 0.0

    try:
        result = await builder.build(semantic_threshold=threshold)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Graph rebuild failed")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ---------- 账户管理 (all roles) ----------

@page_router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, user: dict = Depends(get_current_user)):
    return templates.TemplateResponse(request, "account.html", {
        "request": request, "admin": user,
    })

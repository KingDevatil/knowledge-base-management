"""Shared helpers for admin routes: auth guards, template filters, formatting."""
import os
from urllib.parse import quote
from datetime import datetime, timezone

from fastapi import Request, HTTPException
from fastapi.templating import Jinja2Templates

from config import get_settings
from admin_auth import is_admin_role

# Templates
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)
settings = get_settings()


# ---- Auth guards ----

async def get_current_user(request: Request) -> dict:
    """获取当前登录用户（任意角色）"""
    admin_auth = request.app.state.admin_auth
    return await admin_auth.verify_session(request)


async def require_admin(request: Request) -> dict:
    """获取当前登录用户（仅限管理员）"""
    admin_auth = request.app.state.admin_auth
    user = await admin_auth.verify_session(request)
    if not is_admin_role(user["role"]):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


async def get_current_admin(request: Request) -> dict:
    """向后兼容别名"""
    return await get_current_user(request)


def check_path_access(user: dict, doc_path: str) -> bool:
    """检查用户是否有权访问指定路径下的文档"""
    if is_admin_role(user["role"]):
        return True
    authorized = user.get("authorized_paths", [])
    if not authorized:
        return False
    return any(doc_path == p or doc_path.startswith(p + "/") for p in authorized)


# ---- Template filters ----

def format_datetime(dt_str: str) -> str:
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return dt_str


def format_relative_time(dt_str: str) -> str:
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


templates.env.filters["datetime"] = format_datetime
templates.env.filters["relative_time"] = format_relative_time


def urlencode_filter(s: str) -> str:
    """Jinja2 filter: URL-encode a string for safe use in query parameters."""
    return quote(s or "", safe="")


templates.env.filters["urlencode"] = urlencode_filter

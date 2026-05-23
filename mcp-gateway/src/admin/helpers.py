"""Shared helpers for admin routes: auth, template filters, formatting."""
import os
from datetime import datetime, timezone

from fastapi import Request
from fastapi.templating import Jinja2Templates

from config import get_settings

# Templates
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)
settings = get_settings()


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


# Register template filters
templates.env.filters["datetime"] = format_datetime
templates.env.filters["relative_time"] = format_relative_time

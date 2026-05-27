"""Admin API routes — login authentication and account management."""
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse

from .helpers import templates, settings, get_current_user
from admin_auth import is_admin_role

api_router = APIRouter()


# ---------- 登录 ----------

def _validate_redirect_url(next_url: str, default: str = "/admin/dashboard") -> str:
    """Validate redirect URL is a safe relative path (prevent Open Redirect)."""
    if not next_url:
        return default
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return default


LOGIN_RATE_KEY = "login_rate"
LOGIN_RATE_LIMIT = 5
LOGIN_RATE_WINDOW = 60
LOGIN_LOCKOUT_LIMIT = 15
LOGIN_LOCKOUT_WINDOW = 300


@api_router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin/dashboard"),
):
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

    admin_auth = request.app.state.admin_auth
    account = await admin_auth.authenticate(username, password)
    if not account:
        if attempts == 0:
            await redis.setex(rate_key, LOGIN_RATE_WINDOW, 1)
        else:
            await redis.incr(rate_key)
            if attempts >= LOGIN_RATE_LIMIT:
                await redis.expire(rate_key, LOGIN_LOCKOUT_WINDOW)

        return templates.TemplateResponse(request, "login.html", {
            "request": request, "next": next, "error": "用户名或密码错误",
        }, status_code=401)

    await redis.delete(rate_key)

    token = admin_auth.create_session_token(
        account["username"], account["role"], account.get("authorized_paths", []))

    if not is_admin_role(account["role"]) and next == "/admin/dashboard":
        next = "/admin/documents"

    redirect_url = _validate_redirect_url(next)

    response = RedirectResponse(url=redirect_url, status_code=302)
    # Secure 标志取决于实际请求协议，而非 DEBUG 配置
    # 这样不论 HTTP 还是 HTTPS 都能正常工作（生产环境由 Nginx 决定 HTTPS）
    is_secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(
        key="session", value=token, max_age=settings.SESSION_MAX_AGE,
        httponly=True, secure=is_secure, samesite="lax",
    )
    return response


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

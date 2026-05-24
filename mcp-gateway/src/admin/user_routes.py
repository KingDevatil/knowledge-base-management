"""User management routes (admin only)."""
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse

from .helpers import templates, require_admin

user_router = APIRouter()


@user_router.get("/users", response_class=HTMLResponse)
async def user_list_page(request: Request, user: dict = Depends(require_admin)):
    admin_auth = request.app.state.admin_auth
    accounts = admin_auth.list_accounts()
    return templates.TemplateResponse(request, "users.html", {
        "request": request, "admin": user, "accounts": accounts,
    })


@user_router.post("/api/users/create")
async def user_create(
    request: Request,
    user: dict = Depends(require_admin),
):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    role = body.get("role", "viewer")
    authorized_paths = body.get("authorized_paths", [])

    admin_auth = request.app.state.admin_auth
    success, msg = admin_auth.create_account(username, password, role, authorized_paths)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return JSONResponse({"success": True, "message": msg})


@user_router.post("/api/users/{username}/update")
async def user_update(
    request: Request,
    username: str,
    user: dict = Depends(require_admin),
):
    body = await request.json()
    role = body.get("role")
    authorized_paths = body.get("authorized_paths")
    new_password = body.get("password", "").strip()

    admin_auth = request.app.state.admin_auth
    success, msg = admin_auth.update_account(username, role, authorized_paths)
    if not success:
        raise HTTPException(status_code=400, detail=msg)

    # 如果提供了新密码，同时重置
    if new_password:
        ok, pwd_msg = admin_auth.reset_account_password(username, new_password)
        if not ok:
            raise HTTPException(status_code=400, detail=pwd_msg)

    return JSONResponse({"success": True, "message": msg})


@user_router.post("/api/users/{username}/delete")
async def user_delete(
    request: Request,
    username: str,
    user: dict = Depends(require_admin),
):
    admin_auth = request.app.state.admin_auth
    success, msg = admin_auth.delete_account(username)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return JSONResponse({"success": True, "message": msg})

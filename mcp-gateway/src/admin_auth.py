import json
import os
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import bcrypt
import redis.asyncio as redis
from fastapi import Request, HTTPException, status
from jose import jwt, JWTError

from config import get_settings


def is_admin_role(role: str) -> bool:
    return role in ("super_admin", "admin")


class AdminAuth:
    """用户 Session 认证 + 账户管理"""

    def __init__(self, redis_client: redis.Redis, admin_accounts_file: str, session_secret: str, session_max_age: int = 86400):
        self.redis = redis_client
        self.admin_accounts_file = admin_accounts_file
        self.session_secret = session_secret
        self.session_max_age = session_max_age

    # ---- file IO ----

    def _load_accounts(self) -> dict:
        if not os.path.exists(self.admin_accounts_file):
            return {}
        try:
            with open(self.admin_accounts_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_accounts(self, accounts: dict) -> bool:
        try:
            os.makedirs(os.path.dirname(self.admin_accounts_file), exist_ok=True)
            with open(self.admin_accounts_file, "w", encoding="utf-8") as f:
                json.dump(accounts, f, ensure_ascii=False, indent=2)
            return True
        except IOError:
            return False

    # ---- password ----

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())

    def hash_password(self, plain_password: str) -> str:
        return bcrypt.hashpw(plain_password.encode(), bcrypt.gensalt()).decode()

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _is_session_stale(self, payload: dict, account: dict) -> bool:
        changed_at = account.get("password_changed_at", "")
        if not changed_at:
            return False
        iat = payload.get("iat")
        if not iat:
            return True
        try:
            issued_at = datetime.fromtimestamp(iat, timezone.utc) if isinstance(iat, (int, float)) else datetime.fromisoformat(str(iat).replace("Z", "+00:00"))
            changed = datetime.fromisoformat(str(changed_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return True
        return issued_at < changed

    def ensure_bootstrap_admin(self, username: str = "admin", password: str = "123456") -> bool:
        """Create the first super admin when a fresh Docker data volume is empty."""
        accounts = self._load_accounts()
        if accounts:
            return False
        username = (username or "admin").strip() or "admin"
        password = password or "123456"
        accounts[username] = {
            "username": username,
            "password_hash": self.hash_password(password),
            "role": "super_admin",
            "authorized_paths": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return self._save_accounts(accounts)

    # ---- auth flow ----

    async def authenticate(self, username: str, password: str) -> Optional[dict]:
        accounts = self._load_accounts()
        account = accounts.get(username)
        if not account:
            return None
        if not self.verify_password(password, account["password_hash"]):
            return None
        return {
            "username": username,
            "role": account.get("role", "viewer"),
            "authorized_paths": account.get("authorized_paths", []),
        }

    def create_session_token(self, username: str, role: str, authorized_paths: list = None) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "sub": username,
            "role": role,
            "paths": json.dumps(authorized_paths or []),
            "iat": now,
            "exp": now + timedelta(seconds=self.session_max_age),
            "jti": secrets.token_urlsafe(16),
        }
        return jwt.encode(payload, self.session_secret, algorithm="HS256")

    async def verify_session(self, request: Request) -> dict:
        session_cookie = request.cookies.get("session")
        if not session_cookie:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录",
                                headers={"Location": "/admin/login"})

        try:
            payload = jwt.decode(session_cookie, self.session_secret, algorithms=["HS256"])
            username = payload.get("sub")
            role = payload.get("role", "viewer")
            jti = payload.get("jti", "")
            paths_raw = payload.get("paths", "[]")
            authorized_paths = json.loads(paths_raw) if isinstance(paths_raw, str) else (paths_raw or [])
            account = self._load_accounts().get(username or "", {})
            if not account or self._is_session_stale(payload, account):
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session invalidated")

            blacklisted = await self.redis.get(f"session_blacklist:{jti}")
            if blacklisted:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已失效")

            return {"username": username, "role": role, "jti": jti, "authorized_paths": authorized_paths}
        except JWTError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已过期或无效")

    async def logout(self, request: Request):
        session_cookie = request.cookies.get("session")
        if session_cookie:
            try:
                payload = jwt.decode(session_cookie, self.session_secret, algorithms=["HS256"])
                jti = payload.get("jti", "")
                exp = payload.get("exp", 0)
                now = datetime.now(timezone.utc).timestamp()
                ttl = max(int(exp - now), 1)
                await self.redis.setex(f"session_blacklist:{jti}", ttl, "1")
            except JWTError:
                pass

    # ---- password management ----

    async def change_password(self, username: str, old_password: str, new_password: str) -> tuple:
        accounts = self._load_accounts()
        account = accounts.get(username)
        if not account:
            return False, "用户不存在"
        if not self.verify_password(old_password, account["password_hash"]):
            return False, "当前密码错误"
        account["password_hash"] = self.hash_password(new_password)
        account["password_changed_at"] = self._now_iso()
        if self._save_accounts(accounts):
            return True, "密码修改成功"
        return False, "保存失败，请稍后重试"

    def reset_password(self, username: str, new_password: str) -> tuple:
        accounts = self._load_accounts()
        account = accounts.get(username)
        if not account:
            return False, f"用户 '{username}' 不存在"
        account["password_hash"] = self.hash_password(new_password)
        account["password_changed_at"] = self._now_iso()
        if self._save_accounts(accounts):
            return True, f"用户 '{username}' 的密码已重置"
        return False, "保存失败"

    # ---- account CRUD ----

    def list_accounts(self) -> List[dict]:
        """列出所有账户（不含密码哈希）"""
        accounts = self._load_accounts()
        result = []
        for k, v in accounts.items():
            result.append({
                "username": v.get("username", k),
                "role": v.get("role", "viewer"),
                "authorized_paths": v.get("authorized_paths", []),
                "created_at": v.get("created_at", ""),
            })
        return sorted(result, key=lambda a: a.get("created_at", ""))

    def create_account(self, username: str, password: str, role: str = "viewer",
                       authorized_paths: list = None) -> tuple:
        if not username or len(username) < 2:
            return False, "用户名至少 2 个字符"
        if not password or len(password) < 6:
            return False, "密码至少 6 个字符"
        if role not in ("super_admin", "admin", "user", "viewer"):
            return False, "无效的角色"

        accounts = self._load_accounts()
        if username in accounts:
            return False, "用户名已存在"

        accounts[username] = {
            "username": username,
            "password_hash": self.hash_password(password),
            "role": role,
            "authorized_paths": authorized_paths or [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if self._save_accounts(accounts):
            return True, "创建成功"
        return False, "保存失败"

    def update_account(self, username: str, role: str = None,
                       authorized_paths: list = None) -> tuple:
        accounts = self._load_accounts()
        account = accounts.get(username)
        if not account:
            return False, "用户不存在"

        if role is not None:
            if role not in ("super_admin", "admin", "user", "viewer"):
                return False, "无效的角色"
            # 不允许将最后一个 super_admin 降级
            if account.get("role") == "super_admin" and role != "super_admin":
                super_admins = sum(1 for a in accounts.values() if a.get("role") == "super_admin")
                if super_admins <= 1:
                    return False, "不能降级最后一个超级管理员"

            account["role"] = role
        if authorized_paths is not None:
            account["authorized_paths"] = authorized_paths

        if self._save_accounts(accounts):
            return True, "更新成功"
        return False, "保存失败"

    def delete_account(self, username: str) -> tuple:
        accounts = self._load_accounts()
        account = accounts.get(username)
        if not account:
            return False, "用户不存在"

        # 不可删除最后一个 super_admin
        if account.get("role") == "super_admin":
            super_admins = sum(1 for a in accounts.values() if a.get("role") == "super_admin")
            if super_admins <= 1:
                return False, "不能删除最后一个超级管理员"

        del accounts[username]
        if self._save_accounts(accounts):
            return True, "删除成功"
        return False, "保存失败"

    def reset_account_password(self, username: str, new_password: str) -> tuple:
        """管理员重置用户密码"""
        if not new_password or len(new_password) < 6:
            return False, "密码至少 6 个字符"
        return self.reset_password(username, new_password)

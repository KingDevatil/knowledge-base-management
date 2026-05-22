import json
import os
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt
import redis.asyncio as redis
from fastapi import Request, HTTPException, status
from jose import jwt, JWTError

from config import get_settings


class AdminAuth:
    """管理员 Session 认证"""

    def __init__(self, redis_client: redis.Redis, admin_accounts_file: str, session_secret: str, session_max_age: int = 86400):
        self.redis = redis_client
        self.admin_accounts_file = admin_accounts_file
        self.session_secret = session_secret
        self.session_max_age = session_max_age

    def _load_accounts(self) -> dict:
        if not os.path.exists(self.admin_accounts_file):
            return {}
        try:
            with open(self.admin_accounts_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())

    def hash_password(self, plain_password: str) -> str:
        return bcrypt.hashpw(plain_password.encode(), bcrypt.gensalt()).decode()

    async def authenticate(self, username: str, password: str) -> Optional[dict]:
        """验证管理员账号密码"""
        accounts = self._load_accounts()
        account = accounts.get(username)
        if not account:
            return None
        if not self.verify_password(password, account["password_hash"]):
            return None
        return {
            "username": username,
            "role": account.get("role", "admin"),
        }

    def create_session_token(self, username: str, role: str) -> str:
        """创建 JWT Session Token"""
        now = datetime.now(timezone.utc)
        payload = {
            "sub": username,
            "role": role,
            "iat": now,
            "exp": now + timedelta(seconds=self.session_max_age),
            "jti": secrets.token_urlsafe(16),
        }
        return jwt.encode(payload, self.session_secret, algorithm="HS256")

    async def verify_session(self, request: Request) -> dict:
        """验证 Session，返回用户信息"""
        session_cookie = request.cookies.get("session")
        if not session_cookie:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="未登录",
                headers={"Location": "/admin/login"},
            )

        try:
            payload = jwt.decode(session_cookie, self.session_secret, algorithms=["HS256"])
            username = payload.get("sub")
            role = payload.get("role", "admin")
            jti = payload.get("jti", "")

            # 检查是否在Redis黑名单中（用于登出）
            blacklisted = await self.redis.get(f"session_blacklist:{jti}")
            if blacklisted:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="会话已失效"
                )

            return {"username": username, "role": role, "jti": jti}
        except JWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="会话已过期或无效"
            )

    async def logout(self, request: Request):
        """登出：将Session加入黑名单"""
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

    def _save_accounts(self, accounts: dict) -> bool:
        """保存账户数据到文件"""
        try:
            os.makedirs(os.path.dirname(self.admin_accounts_file), exist_ok=True)
            with open(self.admin_accounts_file, "w", encoding="utf-8") as f:
                json.dump(accounts, f, ensure_ascii=False, indent=2)
            return True
        except IOError:
            return False

    async def change_password(self, username: str, old_password: str, new_password: str) -> tuple[bool, str]:
        """修改密码，返回 (success, message)"""
        accounts = self._load_accounts()
        account = accounts.get(username)
        if not account:
            return False, "用户不存在"
        if not self.verify_password(old_password, account["password_hash"]):
            return False, "当前密码错误"
        account["password_hash"] = self.hash_password(new_password)
        if self._save_accounts(accounts):
            return True, "密码修改成功"
        return False, "保存失败，请稍后重试"

    def reset_password(self, username: str, new_password: str) -> tuple[bool, str]:
        """直接重置密码（用于命令行工具），返回 (success, message)"""
        accounts = self._load_accounts()
        account = accounts.get(username)
        if not account:
            return False, f"用户 '{username}' 不存在"
        account["password_hash"] = self.hash_password(new_password)
        if self._save_accounts(accounts):
            return True, f"用户 '{username}' 的密码已重置"
        return False, "保存失败"

    def require_admin(self, request: Request):
        """依赖注入用：要求管理员权限"""
        # 在路由中使用 await self.verify_session(request)
        pass

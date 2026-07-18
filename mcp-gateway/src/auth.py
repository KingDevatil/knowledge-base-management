import json
import os
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

import redis.asyncio as redis
from fastapi import Request, HTTPException, status

from config import get_settings
from models import APIKeyInfo
from logger import get_logger
from path_permissions import parse_allowed_paths

logger = get_logger()


class APIKeyAuth:
    """API Key 认证与生命周期管理"""

    def __init__(self, redis_client: redis.Redis, api_key_file: str):
        self.redis = redis_client
        self.api_key_file = api_key_file

    def _hash_key(self, key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def _parse_scope(self, raw_scope) -> list[str]:
        if isinstance(raw_scope, list):
            return [str(item) for item in raw_scope]
        if raw_scope is None or raw_scope == "":
            return ["read"]
        if isinstance(raw_scope, bytes):
            raw_scope = raw_scope.decode()
        if isinstance(raw_scope, str):
            try:
                parsed = json.loads(raw_scope)
                if isinstance(parsed, list):
                    return [str(item) for item in parsed]
                if isinstance(parsed, str):
                    return [parsed]
            except json.JSONDecodeError:
                return [raw_scope]
        return ["read"]

    def _path_mode(self, value) -> str:
        value = str(value or "all").strip().lower()
        return "restricted" if value == "restricted" else "all"

    def _extract_api_key(self, request: Request) -> str:
        """Read an API key from Bearer auth or the legacy X-API-Key header."""
        authorization = request.headers.get("Authorization", "").strip()
        legacy_key = request.headers.get("X-API-Key", "").strip()
        bearer_key = ""

        if authorization:
            scheme, separator, credentials = authorization.partition(" ")
            bearer_key = credentials.strip()
            if scheme.lower() != "bearer" or not separator or not bearer_key or " " in bearer_key:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="无效的 Authorization Bearer Token",
                )

        if bearer_key and legacy_key and not secrets.compare_digest(bearer_key, legacy_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization 与 X-API-Key 不一致",
            )

        return bearer_key or legacy_key

    def _load_keys_from_file(self) -> dict:
        if not os.path.exists(self.api_key_file):
            return {}
        try:
            with open(self.api_key_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("api_keys", {})
        except (json.JSONDecodeError, IOError):
            return {}

    async def _save_keys_to_file(self, keys: dict):
        os.makedirs(os.path.dirname(self.api_key_file), exist_ok=True)
        with open(self.api_key_file, "w", encoding="utf-8") as f:
            json.dump({"api_keys": keys}, f, ensure_ascii=False, indent=2)

    async def _load_keys_to_redis(self):
        """将文件中的Key加载到Redis缓存"""
        keys = self._load_keys_from_file()
        for key_hash, info in keys.items():
            redis_key = f"api_key:{key_hash}"
            await self.redis.hset(redis_key, mapping={
                "key_prefix": info.get("key_prefix", ""),
                "applicant": info.get("applicant", ""),
                "applicant_note": info.get("applicant_note", ""),
                "role": info.get("role", "user"),
                "scope": json.dumps(self._parse_scope(info.get("scope", ["read"]))),
                "path_mode": self._path_mode(info.get("path_mode", "all")),
                "allowed_paths": json.dumps(parse_allowed_paths(info.get("allowed_paths", [])), ensure_ascii=False),
                "rate_limit": str(info.get("rate_limit", 30)),
                "status": info.get("status", "active"),
                "duration": info.get("duration", "7d"),
                "created_at": info.get("created_at", ""),
                "expires_at": info.get("expires_at", ""),
                "revoked_at": info.get("revoked_at", "") or "",
                "revoked_by": info.get("revoked_by", "") or "",
                "created_by": info.get("created_by", "admin"),
                "last_used_at": info.get("last_used_at", "") or "",
                "use_count": str(info.get("use_count", 0)),
            })
            # 设置TTL（如果有过期时间）
            expires_at = info.get("expires_at", "")
            if expires_at and info.get("status") == "active":
                try:
                    exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    ttl_seconds = int((exp_dt - now).total_seconds())
                    if ttl_seconds > 0:
                        await self.redis.expire(redis_key, ttl_seconds)
                except ValueError:
                    pass

    async def authenticate(self, request: Request, required_scope: str = "read") -> APIKeyInfo:
        """
        认证流程：
        1. 从 Authorization: Bearer 或 X-API-Key 读取 Key
        2. Redis 查询 Key 元数据
        3. 检查 status: revoked/expired/active
        4. 检查 scope 是否匹配
        5. 限流检查
        6. 更新 last_used_at 和 use_count
        """
        settings = get_settings()
        api_key = self._extract_api_key(request)

        if not api_key or not api_key.startswith("sk-"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="缺少或无效的 API Key"
            )

        key_hash = self._hash_key(api_key)
        redis_key = f"api_key:{key_hash}"

        # 尝试从Redis读取
        info = await self.redis.hgetall(redis_key)
        if not info:
            # 回退到文件加载并同步到Redis
            await self._load_keys_to_redis()
            info = await self.redis.hgetall(redis_key)
            if not info:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="API Key 不存在"
                )

        # 解码字节
        info = {k.decode() if isinstance(k, bytes) else k:
                v.decode() if isinstance(v, bytes) else v
                for k, v in info.items()}

        status_val = info.get("status", "active")

        # 检查吊销
        if status_val == "revoked":
            logger.warning(f"API Key revoked: key_prefix={info.get('key_prefix', '')}, applicant={info.get('applicant', '')}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API Key 已被吊销"
            )

        # 检查过期
        if status_val == "expired":
            logger.warning(f"API Key expired: key_prefix={info.get('key_prefix', '')}, applicant={info.get('applicant', '')}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API Key 已过期"
            )

        expires_at = info.get("expires_at", "")
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) > exp_dt:
                    # 标记为过期
                    await self.redis.hset(redis_key, "status", "expired")
                    await self._sync_redis_to_file()
                    logger.warning(f"API Key auto-expired: key_prefix={info.get('key_prefix', '')}, applicant={info.get('applicant', '')}")
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="API Key 已过期"
                    )
            except ValueError:
                pass

        # 检查权限范围
        scope = self._parse_scope(info.get("scope", "[\"read\"]"))
        if required_scope not in scope:
            logger.warning(f"API Key scope insufficient: key_prefix={info.get('key_prefix', '')}, required={required_scope}, got={scope}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API Key 权限不足"
            )

        # 限流检查
        rate_limit = int(info.get("rate_limit", settings.RATE_LIMIT_DEFAULT))
        rate_key = f"rate_limit:{key_hash}"
        current = await self.redis.incr(rate_key)
        if current == 1:
            await self.redis.expire(rate_key, 60)
        if current > rate_limit:
            logger.warning(f"API Key rate limited: key_prefix={info.get('key_prefix', '')}, count={current}, limit={rate_limit}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="请求过于频繁，请稍后再试"
            )

        # 更新使用统计
        now_str = datetime.now(timezone.utc).isoformat()
        await self.redis.hset(redis_key, "last_used_at", now_str)
        await self.redis.hincrby(redis_key, "use_count", 1)

        return APIKeyInfo(
            key_prefix=info.get("key_prefix", ""),
            applicant=info.get("applicant", ""),
            applicant_note=info.get("applicant_note", ""),
            role=info.get("role", "user"),
            scope=scope,
            path_mode=self._path_mode(info.get("path_mode", "all")),
            allowed_paths=parse_allowed_paths(info.get("allowed_paths", [])),
            rate_limit=rate_limit,
            status=status_val,  # type: ignore
            duration=info.get("duration", "7d"),
            created_at=info.get("created_at", ""),
            expires_at=expires_at,
            revoked_at=info.get("revoked_at") or None,
            revoked_by=info.get("revoked_by") or None,
            created_by=info.get("created_by", "admin"),
            last_used_at=now_str,
            use_count=int(info.get("use_count", 0)) + 1,
        )

    async def create_key(
        self,
        applicant: str,
        applicant_note: str,
        scope: list[str],
        duration: str,
        created_by: str,
        rate_limit: int = 30,
        path_mode: str = "all",
        allowed_paths: list[str] | None = None,
    ) -> str:
        """创建新 Key，返回完整 Key（仅展示一次）"""
        settings = get_settings()
        key_id = secrets.token_urlsafe(32)
        full_key = f"sk-{key_id}"
        key_hash = self._hash_key(full_key)
        key_prefix = full_key[:10]

        now = datetime.now(timezone.utc)
        created_at = now.isoformat()

        # 计算过期时间
        if duration == "permanent":
            expires_at = ""
        elif duration.endswith("d"):
            days = int(duration[:-1])
            expires_at = (now + timedelta(days=days)).isoformat()
        else:
            expires_at = (now + timedelta(days=7)).isoformat()

        info = {
            "key_prefix": key_prefix,
            "applicant": applicant,
            "applicant_note": applicant_note,
            "role": "user",
            "scope": json.dumps(scope),
            "path_mode": self._path_mode(path_mode),
            "allowed_paths": json.dumps(parse_allowed_paths(allowed_paths or []), ensure_ascii=False),
            "rate_limit": str(rate_limit),
            "status": "active",
            "duration": duration,
            "created_at": created_at,
            "expires_at": expires_at,
            "revoked_at": "",
            "revoked_by": "",
            "created_by": created_by,
            "last_used_at": "",
            "use_count": "0",
        }

        # 存入Redis
        redis_key = f"api_key:{key_hash}"
        await self.redis.hset(redis_key, mapping=info)
        if expires_at:
            exp_dt = datetime.fromisoformat(expires_at)
            ttl_seconds = int((exp_dt - now).total_seconds())
            if ttl_seconds > 0:
                await self.redis.expire(redis_key, ttl_seconds)

        # 同步到文件
        await self._sync_redis_to_file()
        logger.info(f"API Key created: prefix={key_prefix}, applicant={applicant}, scope={scope}, duration={duration}, created_by={created_by}")

        return full_key

    async def revoke_key(self, key_hash: str, revoked_by: str) -> bool:
        """吊销 Key"""
        redis_key = f"api_key:{key_hash}"
        exists = await self.redis.exists(redis_key)
        if not exists:
            # 检查文件
            keys = self._load_keys_from_file()
            if key_hash not in keys:
                return False

        now_str = datetime.now(timezone.utc).isoformat()
        await self.redis.hset(redis_key, mapping={
            "status": "revoked",
            "revoked_at": now_str,
            "revoked_by": revoked_by,
        })
        # 移除TTL，保留吊销记录
        await self.redis.persist(redis_key)
        await self._sync_redis_to_file()
        logger.info(f"API Key revoked: key_hash={key_hash}, revoked_by={revoked_by}")
        return True

    async def delete_key(self, key_hash: str) -> bool:
        """永久删除 Key（仅限已吊销的 Key）"""
        redis_key = f"api_key:{key_hash}"
        # 检查是否存在且已吊销
        info = await self.redis.hgetall(redis_key)
        if not info:
            keys = self._load_keys_from_file()
            if key_hash not in keys:
                return False
            info = keys.get(key_hash, {})

        # 解码 bytes
        info_decoded = {
            k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
            for k, v in (info.items() if isinstance(info, dict) else [])
        }
        if info_decoded.get("status") != "revoked":
            return False  # 只能删除已吊销的 Key

        await self.redis.delete(redis_key)
        await self._sync_redis_to_file()
        logger.info(f"API Key deleted: key_hash={key_hash}")
        return True

    async def find_key(self, key_or_prefix: str) -> dict | None:
        """按 hash 或 prefix 查找 Key"""
        keys = await self.list_keys()
        for k in keys:
            if k["key_hash"] == key_or_prefix:
                return k
            if k.get("key_prefix", "") and k["key_prefix"].startswith(key_or_prefix):
                return k
        return None

    async def list_keys(self, status_filter: str | None = None) -> list[dict]:
        """列出所有 Key"""
        pattern = "api_key:*"
        keys = []
        async for key in self.redis.scan_iter(match=pattern):
            info = await self.redis.hgetall(key)
            if not info:
                continue
            info = {k.decode() if isinstance(k, bytes) else k:
                    v.decode() if isinstance(v, bytes) else v
                    for k, v in info.items()}
            key_hash = key.decode().split(":", 1)[1] if isinstance(key, bytes) else key.split(":", 1)[1]

            # 实时检查过期
            if info.get("status") == "active" and info.get("expires_at"):
                try:
                    exp_dt = datetime.fromisoformat(info["expires_at"].replace("Z", "+00:00"))
                    if datetime.now(timezone.utc) > exp_dt:
                        info["status"] = "expired"
                        await self.redis.hset(key, "status", "expired")
                except ValueError:
                    pass

            if status_filter and info.get("status") != status_filter:
                continue

            keys.append({
                "key_hash": key_hash,
                "key_prefix": info.get("key_prefix", ""),
                "applicant": info.get("applicant", ""),
                "applicant_note": info.get("applicant_note", ""),
                "scope": self._parse_scope(info.get("scope", "[\"read\"]")),
                "path_mode": self._path_mode(info.get("path_mode", "all")),
                "allowed_paths": parse_allowed_paths(info.get("allowed_paths", [])),
                "rate_limit": int(info.get("rate_limit", 30)),
                "status": info.get("status", "active"),
                "duration": info.get("duration", "7d"),
                "created_at": info.get("created_at", ""),
                "expires_at": info.get("expires_at", ""),
                "revoked_at": info.get("revoked_at") or None,
                "revoked_by": info.get("revoked_by") or None,
                "created_by": info.get("created_by", "admin"),
                "last_used_at": info.get("last_used_at") or None,
                "use_count": int(info.get("use_count", 0)),
            })

        # 同时扫描文件中的Key（可能被Redis清理了过期项）
        file_keys = self._load_keys_from_file()
        seen_hashes = {k["key_hash"] for k in keys}
        for key_hash, info in file_keys.items():
            if key_hash in seen_hashes:
                continue
            if status_filter and info.get("status") != status_filter:
                continue
            keys.append({
                "key_hash": key_hash,
                **{k: v for k, v in info.items() if k != "scope"},
                "scope": self._parse_scope(info.get("scope", ["read"])),
                "path_mode": self._path_mode(info.get("path_mode", "all")),
                "allowed_paths": parse_allowed_paths(info.get("allowed_paths", [])),
                "use_count": info.get("use_count", 0),
            })

        keys.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return keys

    async def _sync_redis_to_file(self):
        """将Redis中的所有Key同步到持久化文件"""
        pattern = "api_key:*"
        all_keys = {}
        async for key in self.redis.scan_iter(match=pattern):
            info = await self.redis.hgetall(key)
            if not info:
                continue
            info = {k.decode() if isinstance(k, bytes) else k:
                    v.decode() if isinstance(v, bytes) else v
                    for k, v in info.items()}
            key_hash = key.decode().split(":", 1)[1] if isinstance(key, bytes) else key.split(":", 1)[1]
            all_keys[key_hash] = info

        await self._save_keys_to_file(all_keys)

import asyncio
from contextlib import suppress
import redis.asyncio as redis
import uuid


class WriteLockError(Exception):
    """写入锁获取失败"""

    def __init__(self, message: str, retry_after_ms: int = 0):
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class WriteLock:
    def __init__(
        self,
        redis_client: redis.Redis,
        key: str = "kb:write_lock",
        ttl: int = 30,
        renew_interval: float | None = None,
    ):
        self.redis = redis_client
        self.key = key
        self.ttl = ttl
        self.renew_interval = renew_interval or max(ttl / 3, 0.1)
        self._lock_id: str | None = None
        self._renew_task: asyncio.Task | None = None
        self._lease_lost = False

    async def acquire(self) -> bool:
        lock_id = str(uuid.uuid4())
        acquired = await self.redis.set(
            self.key, lock_id, nx=True, ex=self.ttl
        )
        if acquired:
            self._lock_id = lock_id
        return bool(acquired)

    async def release(self) -> None:
        renew_task = self._renew_task
        self._renew_task = None
        if renew_task:
            renew_task.cancel()
            with suppress(asyncio.CancelledError):
                await renew_task

        lock_id = self._lock_id
        if not lock_id:
            return
        lua = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
        """
        await self.redis.eval(lua, 1, self.key, lock_id)
        if self._lock_id == lock_id:
            self._lock_id = None

    def _start_renewal(self) -> None:
        self._lease_lost = False
        self._renew_task = asyncio.create_task(self._renew_lease(self._lock_id))

    async def _renew_lease(self, lock_id: str | None) -> None:
        if not lock_id:
            return
        lua = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("expire", KEYS[1], ARGV[2])
            else
                return 0
            end
        """
        try:
            while self._lock_id == lock_id:
                await asyncio.sleep(self.renew_interval)
                renewed = await self.redis.eval(lua, 1, self.key, lock_id, self.ttl)
                if not renewed:
                    self._lease_lost = True
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lease_lost = True

    async def __aenter__(self):
        if not await self.acquire():
            raise WriteLockError(
                "获取写入锁失败，请稍后重试",
                retry_after_ms=max(1, self.ttl) * 1000,
            )
        self._start_renewal()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.release()

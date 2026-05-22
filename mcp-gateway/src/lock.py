import redis.asyncio as redis
import uuid


class WriteLockError(Exception):
    """写入锁获取失败"""
    pass


class WriteLock:
    def __init__(self, redis_client: redis.Redis, key: str = "kb:write_lock", ttl: int = 30):
        self.redis = redis_client
        self.key = key
        self.ttl = ttl
        self._lock_id: str | None = None

    async def acquire(self) -> bool:
        self._lock_id = str(uuid.uuid4())
        acquired = await self.redis.set(
            self.key, self._lock_id, nx=True, ex=self.ttl
        )
        return bool(acquired)

    async def release(self) -> None:
        if not self._lock_id:
            return
        lua = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
        """
        await self.redis.eval(lua, 1, self.key, self._lock_id)
        self._lock_id = None

    async def __aenter__(self):
        if not await self.acquire():
            raise WriteLockError("获取写入锁失败，请稍后重试")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.release()

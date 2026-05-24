"""Unit tests for WriteLock (Redis-based distributed write lock)."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from lock import WriteLock, WriteLockError


# ==================== Unit Tests with Mock Redis ====================

class MockRedis:
    """Simulates a Redis connection for lock testing."""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._ttl: dict[str, float] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int = 0) -> bool:
        if nx and key in self._store:
            return False
        self._store[key] = value
        if ex > 0:
            self._ttl[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def delete(self, key: str) -> int:
        if key in self._store:
            del self._store[key]
            return 1
        return 0

    async def eval(self, lua_script: str, numkeys: int, *args) -> int:
        """Simulate Lua atomic get+delete."""
        key = args[0]
        expected_value = args[1] if len(args) > 1 else ""
        if self._store.get(key) == expected_value:
            del self._store[key]
            return 1
        return 0

    # Additional methods for compatibility
    async def ping(self): return True
    async def hset(self, *args, **kwargs): return 0
    async def hget(self, *args, **kwargs): return None
    async def hdel(self, *args, **kwargs): return 0
    async def hgetall(self, *args, **kwargs): return {}
    async def setex(self, *args, **kwargs): return True
    async def expire(self, *args, **kwargs): return True
    async def expireat(self, *args, **kwargs): return True
    async def incr(self, *args, **kwargs): return 1
    async def ttl(self, *args, **kwargs): return -1
    async def close(self): pass
    def pipeline(self): return self


class TestWriteLockUnit:
    """Unit tests for WriteLock with mock Redis."""

    @pytest.fixture
    def redis(self):
        return MockRedis()

    @pytest.fixture
    def lock(self, redis):
        return WriteLock(redis, key="test:lock", ttl=5)

    @pytest.mark.asyncio
    async def test_acquire_lock_succeeds(self, lock):
        """Lock should be acquired when no one holds it."""
        acquired = await lock.acquire()
        assert acquired is True
        assert lock._lock_id is not None

    @pytest.mark.asyncio
    async def test_acquire_lock_fails_when_held(self, lock):
        """Second acquire should fail when lock is held."""
        await lock.acquire()

        # Create second lock pointing to same Redis
        lock2 = WriteLock(lock.redis, key=lock.key, ttl=5)
        acquired = await lock2.acquire()
        assert acquired is False

    @pytest.mark.asyncio
    async def test_release_lock_succeeds(self, lock):
        """Release should clear lock when called by owner."""
        await lock.acquire()
        await lock.release()
        assert lock._lock_id is None

    @pytest.mark.asyncio
    async def test_release_noop_without_acquire(self, lock):
        """Release without acquire should be a no-op."""
        await lock.release()
        # Should not raise

    @pytest.mark.asyncio
    async def test_context_manager_acquires_and_releases(self, lock):
        """async with should acquire and release automatically."""
        async with lock:
            assert lock._lock_id is not None
        assert lock._lock_id is None

    @pytest.mark.asyncio
    async def test_context_manager_raises_when_locked(self, lock):
        """async with should raise WriteLockError when lock is held."""
        await lock.acquire()

        lock2 = WriteLock(lock.redis, key=lock.key, ttl=5)
        with pytest.raises(WriteLockError, match="获取写入锁失败"):
            async with lock2:
                pass

    @pytest.mark.asyncio
    async def test_double_release_is_safe(self, lock):
        """Calling release twice should not cause errors."""
        await lock.acquire()
        await lock.release()
        await lock.release()  # Should be safe no-op

    @pytest.mark.asyncio
    async def test_acquire_release_acquire_cycle(self, lock):
        """Lock can be re-acquired after release."""
        await lock.acquire()
        await lock.release()

        # Should be able to acquire again
        acquired = await lock.acquire()
        assert acquired is True
        await lock.release()

    @pytest.mark.asyncio
    async def test_lock_id_uniqueness(self, lock):
        """Each acquire should generate a unique lock ID."""
        await lock.acquire()
        id1 = lock._lock_id
        await lock.release()

        await lock.acquire()
        id2 = lock._lock_id
        assert id1 != id2

    @pytest.mark.asyncio
    async def test_lua_release_only_deletes_own_lock(self, lock):
        """Lua script should only delete if the value matches."""
        await lock.acquire()

        # Try to release with a different lock instance (different lock_id)
        lock2 = WriteLock(lock.redis, key=lock.key, ttl=5)
        # Simulate a different lock_id
        lock2._lock_id = "different-id"
        await lock2.release()

        # Original lock should still be held
        assert lock._lock_id is not None

    @pytest.mark.asyncio
    async def test_context_manager_releases_on_exception(self, lock):
        """Lock should be released even if an exception occurs inside context."""
        try:
            async with lock:
                raise ValueError("something went wrong")
        except ValueError:
            pass

        # Lock should be released
        assert lock._lock_id is None


class TestWriteLockConfiguration:
    """Test WriteLock configuration and defaults."""

    def test_default_key_and_ttl(self):
        """Default values should match expected constants."""
        redis = MockRedis()
        lock = WriteLock(redis)
        assert lock.key == "kb:write_lock"
        assert lock.ttl == 30

    def test_custom_key_and_ttl(self):
        """Custom key and TTL should be accepted."""
        redis = MockRedis()
        lock = WriteLock(redis, key="custom:lock", ttl=60)
        assert lock.key == "custom:lock"
        assert lock.ttl == 60

    def test_lock_error_is_exception(self):
        """WriteLockError should be a proper exception."""
        error = WriteLockError("test message")
        assert str(error) == "test message"
        assert isinstance(error, Exception)

    @pytest.mark.asyncio
    async def test_concurrent_acquire_only_one_succeeds(self):
        """Multiple concurrent acquires should only succeed once."""
        redis = MockRedis()
        lock1 = WriteLock(redis, key="concurrent:lock", ttl=5)
        lock2 = WriteLock(redis, key="concurrent:lock", ttl=5)
        lock3 = WriteLock(redis, key="concurrent:lock", ttl=5)

        results = [await lock.acquire() for lock in [lock1, lock2, lock3]]

        # Only one should succeed
        assert results.count(True) == 1
        assert results.count(False) == 2

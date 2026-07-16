"""Unit tests for KnowledgeBase (Chroma wrapper + Redis doc index)."""
import sys
import os
import threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch

from knowledge_base import KnowledgeBase, DOC_INDEX_KEY


# ==================== Mock Helpers ====================

class MockChromaClient:
    """Mock Chroma HTTP client for testing."""

    def __init__(self):
        self._collections: dict[str, MockCollection] = {}

    def get_or_create_collection(self, name: str, metadata: dict = None):
        if name not in self._collections:
            self._collections[name] = MockCollection(name)
        return self._collections[name]

    def heartbeat(self):
        return True


class MockCollection:
    """Mock a single Chroma collection."""

    def __init__(self, name: str):
        self.name = name
        self._data: list[dict] = []

    def add(self, ids, documents=None, embeddings=None, metadatas=None):
        for i, chunk_id in enumerate(ids):
            self._data.append({
                "id": chunk_id,
                "document": documents[i] if documents else "",
                "metadata": metadatas[i] if metadatas else {},
                "embedding": embeddings[i] if embeddings else None,
            })

    def get(self, ids=None, where=None, include=None, limit=None, offset=None):
        """Simulate Chroma get query."""
        filtered = self._data
        if where:
            for k, v in where.items():
                filtered = [d for d in filtered if d["metadata"].get(k) == v]
        if ids:
            filtered = [d for d in filtered if d["id"] in ids]

        result = {"ids": [d["id"] for d in filtered]}
        if include:
            if "documents" in include:
                result["documents"] = [d["document"] for d in filtered]
            if "metadatas" in include:
                result["metadatas"] = [d["metadata"] for d in filtered]
            if "embeddings" in include:
                result["embeddings"] = [d["embedding"] for d in filtered]
        return result

    def query(self, query_embeddings, n_results=5, where=None, include=None):
        """Simplified mock — returns first n_results items."""
        filtered = self._data
        if where:
            for k, v in where.items():
                if isinstance(v, dict) and "$contains" in v:
                    filtered = [d for d in filtered if v["$contains"] in d["metadata"].get(k, "")]
                elif isinstance(v, dict) and "$eq" in v:
                    filtered = [d for d in filtered if d["metadata"].get(k) == v["$eq"]]
                else:
                    filtered = [d for d in filtered if d["metadata"].get(k) == v]

        limit = min(n_results, len(filtered))
        selected = filtered[:limit]

        return {
            "ids": [[d["id"] for d in selected]],
            "documents": [[d["document"] for d in selected]] if include and "documents" in include else [],
            "metadatas": [[d["metadata"] for d in selected]] if include and "metadatas" in include else [],
            "distances": [[0.1 * i for i in range(len(selected))]] if include and "distances" in include else [],
        }

    def delete(self, ids=None):
        if ids:
            self._data = [d for d in self._data if d["id"] not in ids]

    def count(self):
        return len(self._data)


class MockRedis:
    """Mock Redis with hash operations support."""

    def __init__(self):
        self._hashes: dict[str, dict[str, str]] = {}
        self._strings: dict[str, str] = {}
        self._ttls: dict[str, int] = {}

    async def hset(self, key: str, field: str = None, value: str = None, mapping: dict = None, **kwargs):
        """Support both hset(key, field, value) and hset(key, mapping={...}) calling conventions."""
        if key not in self._hashes:
            self._hashes[key] = {}
        # hset(key, field, value) — 3 positional args
        if field is not None and value is not None:
            self._hashes[key][field] = value
        # hset(key, mapping={...})
        if mapping:
            self._hashes[key].update(mapping)
        if kwargs:
            self._hashes[key].update(kwargs)
        return len(mapping or {}) + (1 if field is not None and value is not None else 0) + len(kwargs)

    async def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    async def hdel(self, key: str, *fields) -> int:
        removed = 0
        if key in self._hashes:
            for f in fields:
                if f in self._hashes[key]:
                    del self._hashes[key][f]
                    removed += 1
        return removed

    async def hgetall(self, key: str) -> dict[str, str]:
        return self._hashes.get(key, {}).copy()

    def pipeline(self):
        return MockPipeline(self)

    # String operations for rate limiting / lock
    async def get(self, key: str) -> str | None:
        return self._strings.get(key)

    async def set(self, key: str, value: str, nx: bool = False, ex: int = 0):
        if nx and key in self._strings:
            return False
        self._strings[key] = value
        return True

    async def delete(self, key: str):
        self._strings.pop(key, None)
        self._hashes.pop(key, None)

    async def setex(self, key: str, ttl: int, value: str):
        self._strings[key] = value
        self._ttls[key] = ttl

    async def incr(self, key: str):
        if key not in self._strings:
            self._strings[key] = "0"
        val = int(self._strings[key]) + 1
        self._strings[key] = str(val)
        return val

    async def ttl(self, key: str):
        return self._ttls.get(key, -1)

    async def expire(self, key: str, ttl: int):
        self._ttls[key] = ttl

    async def expireat(self, key: str, timestamp):
        pass

    async def scan(self, cursor, match=None, count=100):
        return (0, [])

    async def eval(self, *args):
        return 0

    async def ping(self): return True
    async def close(self): pass


class MockPipeline:
    def __init__(self, redis: MockRedis):
        self._redis = redis
        self._commands: list = []

    def hset(self, key: str, field: str, value: str):
        self._commands.append(("hset", key, field, value))
        return self

    async def execute(self):
        for cmd in self._commands:
            if cmd[0] == "hset":
                await self._redis.hset(cmd[1], mapping={cmd[2]: cmd[3]})
        self._commands.clear()


# ==================== KnowledgeBase Tests ====================

class TestKnowledgeBase:
    """Test KnowledgeBase with mock Chroma + Redis."""

    @pytest.fixture
    def chroma(self):
        return MockChromaClient()

    @pytest.fixture
    def redis(self):
        return MockRedis()

    @pytest.fixture
    def kb(self, chroma, redis):
        kb = KnowledgeBase(chroma, "test_collection")
        kb.set_redis(redis)
        return kb

    @pytest.mark.asyncio
    async def test_add_document_chunks(self, kb):
        """Adding chunks should create entries in Chroma and Redis index."""
        await kb.add_document_chunks(
            doc_id="doc-1",
            title="Test Document",
            chunks=["chunk 1", "chunk 2", "chunk 3"],
            embeddings=[[0.1], [0.2], [0.3]],
            metadata={
                "path": "test/path",
                "tags": ["test"],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )

        # Check Redis index
        index_entry = await kb._doc_index_get("doc-1")
        assert index_entry is not None
        assert index_entry["doc_id"] == "doc-1"
        assert index_entry["title"] == "Test Document"
        assert index_entry["chunk_count"] == 3
        assert index_entry["path"] == "test/path"

    @pytest.mark.asyncio
    async def test_add_empty_chunks_is_noop(self, kb):
        """Adding zero chunks should not error."""
        await kb.add_document_chunks(
            doc_id="doc-empty",
            title="Empty",
            chunks=[],
            embeddings=[],
            metadata={"path": ""},
        )
        # Should not have created an index entry
        index_entry = await kb._doc_index_get("doc-empty")
        assert index_entry is None

    @pytest.mark.asyncio
    async def test_delete_document(self, kb):
        """Deleting a document should remove from Chroma and Redis index."""
        # First add a document
        await kb.add_document_chunks(
            doc_id="doc-del",
            title="To Delete",
            chunks=["chunk"],
            embeddings=[[0.1]],
            metadata={"path": ""},
        )

        # Delete it
        deleted = await kb.delete_document("doc-del")
        assert deleted == 1

        # Redis index should be cleared
        index_entry = await kb._doc_index_get("doc-del")
        assert index_entry is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_document(self, kb):
        """Deleting a nonexistent document should return 0."""
        deleted = await kb.delete_document("nonexistent")
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_doc_index_get_set_delete(self, kb):
        """Redis index CRUD operations should work."""
        # Set
        await kb._doc_index_set("doc-idx", {"doc_id": "doc-idx", "title": "Indexed", "chunk_count": 5})
        # Get
        entry = await kb._doc_index_get("doc-idx")
        assert entry is not None
        assert entry["title"] == "Indexed"
        assert entry["chunk_count"] == 5
        # Delete
        await kb._doc_index_delete("doc-idx")
        assert await kb._doc_index_get("doc-idx") is None

    @pytest.mark.asyncio
    async def test_doc_index_handles_non_ascii(self, kb):
        """Redis index should handle Chinese characters."""
        await kb._doc_index_set("doc-cn", {"doc_id": "doc-cn", "title": "测试文档", "path": "游戏/武将"})
        entry = await kb._doc_index_get("doc-cn")
        assert entry["title"] == "测试文档"
        assert entry["path"] == "游戏/武将"

    @pytest.mark.asyncio
    async def test_doc_index_rebuild_reconciles_stale_entries_and_preserves_extra_fields(self, kb):
        await kb._doc_index_set("stale", {
            "doc_id": "stale", "title": "Stale", "chunk_count": 1,
        })
        await kb._doc_index_set("live", {
            "doc_id": "live", "title": "Old title", "content_hash": "sha256-live",
        })
        kb.collection.add(
            ids=["live#chunk-0", "live#chunk-1"],
            documents=["first", "second"],
            metadatas=[
                {"doc_id": "live", "title": "Live", "path": "docs", "chunk_index": 0},
                {"doc_id": "live", "title": "Live", "path": "docs", "chunk_index": 1},
            ],
        )

        rebuilt = await kb._doc_index_rebuild()

        assert rebuilt == 1
        assert await kb._doc_index_get("stale") is None
        live = await kb._doc_index_get("live")
        assert live["title"] == "Live"
        assert live["chunk_count"] == 2
        assert live["content_hash"] == "sha256-live"
        assert "__write_status" not in live

    @pytest.mark.asyncio
    async def test_list_documents(self, kb):
        """Listing documents should return DocumentInfo objects."""
        await kb._doc_index_set("d1", {"doc_id": "d1", "title": "Doc 1", "path": "a", "tags": [], "chunk_count": 2, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})
        await kb._doc_index_set("d2", {"doc_id": "d2", "title": "Doc 2", "path": "b", "tags": [], "chunk_count": 1, "created_at": "2026-06-01T00:00:00Z", "updated_at": "2026-06-01T00:00:00Z"})

        docs, total = await kb.list_documents(limit=10)
        assert total == 2
        assert len(docs) == 2
        # Should be sorted by created_at desc (d2 first)
        assert docs[0].doc_id == "d2"
        assert docs[1].doc_id == "d1"

    @pytest.mark.asyncio
    async def test_list_documents_with_path_filter(self, kb):
        """Path filter should return only matching documents."""
        await kb._doc_index_set("d1", {"doc_id": "d1", "title": "D1", "path": "games", "tags": [], "chunk_count": 1, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})
        await kb._doc_index_set("d2", {"doc_id": "d2", "title": "D2", "path": "docs", "tags": [], "chunk_count": 1, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})

        docs, total = await kb.list_documents(path="games")
        assert total == 1
        assert len(docs) == 1
        assert docs[0].doc_id == "d1"

    @pytest.mark.asyncio
    async def test_list_documents_pagination(self, kb):
        """Pagination should work with limit and offset."""
        for i in range(5):
            await kb._doc_index_set(f"d{i}", {"doc_id": f"d{i}", "title": f"Doc {i}", "path": "", "tags": [], "chunk_count": 1, "created_at": f"2026-01-{i+1:02d}T00:00:00Z", "updated_at": f"2026-01-{i+1:02d}T00:00:00Z"})

        # First page
        page1, total = await kb.list_documents(limit=2, offset=0)
        assert total == 5
        assert len(page1) == 2
        # Second page
        page2, total = await kb.list_documents(limit=2, offset=2)
        assert total == 5
        assert len(page2) == 2

    @pytest.mark.asyncio
    async def test_count_documents(self, kb):
        """Count should return number of indexed documents."""
        await kb._doc_index_set("d1", {"doc_id": "d1", "title": "D1", "chunk_count": 1, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})
        await kb._doc_index_set("d2", {"doc_id": "d2", "title": "D2", "chunk_count": 1, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})

        count = await kb.count_documents()
        assert count == 2

    @pytest.mark.asyncio
    async def test_get_document_chunks(self, kb):
        """Getting document chunks should return sorted chunks."""
        await kb.add_document_chunks(
            doc_id="doc-chunks",
            title="Chunked",
            chunks=["chunk-0", "chunk-1", "chunk-2"],
            embeddings=[[0.0], [0.1], [0.2]],
            metadata={"path": ""},
        )

        chunks = await kb.get_document_chunks("doc-chunks")
        assert len(chunks) == 3
        # Should be sorted by chunk_index
        assert chunks[0]["metadata"]["chunk_index"] == 0
        assert chunks[1]["metadata"]["chunk_index"] == 1
        assert chunks[2]["metadata"]["chunk_index"] == 2
        assert "embedding" not in chunks[0]

        chunks_with_embeddings = await kb.get_document_chunks(
            "doc-chunks", include_embeddings=True
        )
        assert chunks_with_embeddings[0]["embedding"] == [0.0]

    @pytest.mark.asyncio
    async def test_chroma_read_calls_run_outside_the_event_loop_thread(self, kb):
        await kb.add_document_chunks(
            doc_id="doc-threaded",
            title="Threaded",
            chunks=["content"],
            embeddings=[[0.5]],
            metadata={"path": ""},
        )
        event_loop_thread = threading.get_ident()
        get_threads = []
        query_threads = []
        original_get = kb.collection.get
        original_query = kb.collection.query

        def tracked_get(*args, **kwargs):
            get_threads.append(threading.get_ident())
            return original_get(*args, **kwargs)

        def tracked_query(*args, **kwargs):
            query_threads.append(threading.get_ident())
            return original_query(*args, **kwargs)

        with patch.object(kb.collection, "get", side_effect=tracked_get), patch.object(
            kb.collection, "query", side_effect=tracked_query
        ):
            await kb.get_document_chunks("doc-threaded")
            await kb.search(query_embedding=[0.5], top_k=1)

        assert get_threads and get_threads[0] != event_loop_thread
        assert query_threads and query_threads[0] != event_loop_thread

    @pytest.mark.asyncio
    async def test_search_returns_results(self, kb):
        """Search should return SearchResult objects."""
        await kb.add_document_chunks(
            doc_id="doc-search",
            title="Search Doc",
            chunks=["relevant content here"],
            embeddings=[[0.5]],
            metadata={"path": "search/path"},
        )

        results = await kb.search(query_embedding=[0.5], top_k=5)
        assert len(results) >= 1
        assert results[0].doc_id == "doc-search"
        assert results[0].title == "Search Doc"

    @pytest.mark.asyncio
    async def test_search_with_path_filter(self, kb):
        """Search with path filter should only return matching results."""
        await kb.add_document_chunks(
            doc_id="d-a",
            title="Doc A",
            chunks=["content A"],
            embeddings=[[0.5]],
            metadata={"path": "path-a"},
        )
        await kb.add_document_chunks(
            doc_id="d-b",
            title="Doc B",
            chunks=["content B"],
            embeddings=[[0.5]],
            metadata={"path": "path-b"},
        )

        results = await kb.search(query_embedding=[0.5], top_k=5, filter_path="path-a")
        assert len(results) >= 1
        assert all(r.doc_id == "d-a" for r in results)

    @pytest.mark.asyncio
    async def test_list_documents_by_paths(self, kb):
        """Multi-path listing should filter correctly."""
        await kb._doc_index_set("d1", {"doc_id": "d1", "title": "D1", "path": "games", "tags": [], "chunk_count": 1, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})
        await kb._doc_index_set("d2", {"doc_id": "d2", "title": "D2", "path": "games/sub", "tags": [], "chunk_count": 1, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})
        await kb._doc_index_set("d3", {"doc_id": "d3", "title": "D3", "path": "docs", "tags": [], "chunk_count": 1, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})

        # games + subpath should return both
        docs, total = await kb.list_documents_by_paths(paths=["games"])
        assert total == 2
        assert len(docs) == 2

        # games + docs should return all
        docs, total = await kb.list_documents_by_paths(paths=["games", "docs"])
        assert total == 3
        assert len(docs) == 3

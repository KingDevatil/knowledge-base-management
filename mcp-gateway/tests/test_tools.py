"""Unit tests for helpers, KnowledgeToolsReader, and KnowledgeTools."""
import asyncio
import sys
import os

# Must set DEBUG before any project imports to bypass SESSION_SECRET check
os.environ["DEBUG"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from helpers import content_hash, content_size_kb
from document_versions import DocumentVersionStore
from tools_reader import KnowledgeToolsReader
from tools import KnowledgeTools
from lock import WriteLockError


# ==================== Mock Services ====================

class MockRedis:
    """In-memory Redis mock for write lock and doc index cache."""
    def __init__(self):
        self._store = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, nx=False, ex=0):
        if nx and key in self._store:
            return False
        self._store[key] = value
        return True

    async def delete(self, key):
        self._store.pop(key, None)
        return 1

    async def exists(self, key):
        return 1 if key in self._store else 0

    async def expire(self, key, ttl):
        return True

    async def incr(self, key):
        value = int(self._store.get(key, 0)) + 1
        self._store[key] = str(value)
        return value

    async def ping(self):
        return True

    def close(self):
        pass


class MockWriteLock:
    """Mock write lock that always succeeds."""
    def __init__(self):
        self._locked = False

    async def __aenter__(self):
        self._locked = True
        return self

    async def __aexit__(self, *args):
        self._locked = False


class MockEmbedder:
    """Mock Ollama embedder."""
    def __init__(self, fail: bool = False):
        self.fail = fail

    async def embed_single(self, text: str) -> list[float]:
        if self.fail:
            return []
        return [0.1] * 128

    async def embed(self, chunks: list[str]) -> list[list[float]]:
        if self.fail:
            return []
        return [[0.1] * 128 for _ in chunks]

    async def health_check(self):
        return not self.fail

    async def close(self):
        pass


class MockSourceStore:
    """Mock source file storage — stores content by doc_id for reliable retrieval."""
    def __init__(self):
        self._store = {}       # doc_id -> content
        self._source_paths = {}  # doc_id -> source_path
        self.fail_delete_source_path = False

    def save_source(self, doc_id, content, path=""):
        self._store[doc_id] = content
        source_path = f"documents/{path}/{doc_id}/source.md"
        self._source_paths[doc_id] = source_path
        return source_path

    def get_source(self, doc_id, path=""):
        return self._store.get(doc_id, "")

    def get_source_by_full_path(self, source_path: str) -> str:
        for doc_id, sp in self._source_paths.items():
            if sp == source_path:
                return self._store.get(doc_id, "")
        return ""

    def delete_source(self, doc_id, path=""):
        self._store.pop(doc_id, None)
        self._source_paths.pop(doc_id, None)

    def delete_source_by_path(self, source_path: str):
        if self.fail_delete_source_path:
            self.fail_delete_source_path = False
            raise RuntimeError("simulated source cleanup failure")
        for doc_id, sp in list(self._source_paths.items()):
            if sp == source_path:
                self._store.pop(doc_id, None)
                self._source_paths.pop(doc_id, None)
                return

    def move_source(self, doc_id, old_path, new_path):
        pass  # content stays the same, only path metadata changes

    def list_all_documents(self):
        return [{"doc_id": doc_id} for doc_id in self._store]

    def source_exists(self, doc_id, path=""):
        return doc_id in self._store


class MockChromaCollection:
    """Mock Chroma collection for testing."""
    def __init__(self):
        self._docs = {}  # id -> {metadata, document}
        self.fail_next_add = False

    def add(self, ids, documents, metadatas, embeddings=None):
        if self.fail_next_add:
            self.fail_next_add = False
            raise RuntimeError("simulated staging add failure")
        for i, doc_id in enumerate(ids):
            self._docs[doc_id] = {
                "id": doc_id,
                "metadata": metadatas[i] if metadatas else {},
                "document": documents[i] if documents else "",
                "embedding": embeddings[i] if embeddings else None,
            }

    def get(self, ids=None, where=None, limit=None):
        if ids:
            results = [self._docs.get(i, {}) for i in ids if i in self._docs]
        elif where:
            results = [
                value for value in self._docs.values()
                if all(value.get("metadata", {}).get(key) == expected for key, expected in where.items())
            ]
        else:
            results = list(self._docs.values())
        if limit:
            results = results[:limit]
        return {
            "ids": [r.get("id", "") for r in results],
            "metadatas": [r.get("metadata", {}) for r in results],
            "documents": [r.get("document", "") for r in results],
        }

    def update(self, ids, metadatas=None, documents=None):
        for i, doc_id in enumerate(ids):
            if doc_id in self._docs:
                if metadatas:
                    self._docs[doc_id]["metadata"] = metadatas[i]
                if documents:
                    self._docs[doc_id]["document"] = documents[i]

    def delete(self, ids):
        for doc_id in ids:
            self._docs.pop(doc_id, None)

    def count(self):
        return len(self._docs)

    def peek(self, limit=10):
        return {
            "ids": list(self._docs.keys())[:limit],
            "metadatas": [v["metadata"] for v in list(self._docs.values())[:limit]],
            "documents": [v["document"] for v in list(self._docs.values())[:limit]],
        }


class MockChromaClient:
    """Mock Chroma HTTP client."""
    def __init__(self):
        self._collections = {}

    def get_or_create_collection(self, name, metadata=None):
        if name not in self._collections:
            self._collections[name] = MockChromaCollection()
        return self._collections[name]

    def heartbeat(self):
        return True


class MockKnowledgeBase:
    """Mock KnowledgeBase (Chroma wrapper + Redis doc index)."""
    def __init__(self):
        self.collection = MockChromaCollection()
        self._redis = MockRedis()
        self._doc_index = {}
        self.fail_next_add_chunks = False

    def set_redis(self, redis_client):
        self._redis = redis_client

    async def _doc_index_get(self, doc_id):
        return self._doc_index.get(doc_id)

    async def _doc_index_set(self, doc_id, data):
        self._doc_index[doc_id] = data

    async def _doc_index_all(self):
        return [v for k, v in self._doc_index.items()]

    async def list_documents(self, tags=None, path="", limit=20, offset=0):
        from models import DocumentInfo
        items = [v for k, v in self._doc_index.items()]
        if path:
            items = [d for d in items if d.get("path", "") == path or d.get("path", "").startswith(path + "/")]
        if tags:
            items = [d for d in items if any(t in d.get("tags", []) for t in tags)]
        total = len(items)
        items = items[offset:offset + limit]
        return [DocumentInfo(**item) for item in items], total

    async def search(self, query_embedding, top_k=5, filter_tags=None, filter_path=""):
        from models import SearchResult
        results = []
        for doc_id, info in self._doc_index.items():
            if filter_path and not info.get("path", "").startswith(filter_path):
                continue
            results.append(SearchResult(
                content=info.get("content", "")[:50],
                title=info.get("title", ""),
                path=info.get("path", ""),
                doc_id=doc_id,
                score=0.95,
            ))
        return results[:top_k]

    async def get_document_chunks(self, doc_id, include_embeddings=False):
        if doc_id not in self.collection._docs:
            return []
        doc = self.collection._docs[doc_id]
        return [{
            "id": doc_id,
            "metadata": dict(doc["metadata"]),
            "content": doc["document"],
            "embedding": doc.get("embedding", [0.1] * 128),
        }]

    async def add_document_chunks(self, doc_id, title, chunks, embeddings, metadata):
        if self.fail_next_add_chunks:
            self.fail_next_add_chunks = False
            raise RuntimeError("simulated Chroma add failure")
        full_content = "\n\n".join(chunks)
        chunk_metadata = {
            **metadata,
            "doc_id": doc_id,
            "title": title,
            "chunk_index": 0,
            "total_chunks": len(chunks),
        }
        self.collection.add(
            ids=[doc_id],
            documents=[full_content],
            metadatas=[chunk_metadata],
            embeddings=[embeddings[0]] if embeddings else None,
        )
        self._doc_index[doc_id] = {
            "doc_id": doc_id,
            "title": title,
            "path": metadata.get("path", ""),
            "tags": metadata.get("tags", []),
            "header_tags": metadata.get("header_tags", []),
            "entities": metadata.get("entities", []),
            "chunk_count": len(chunks),
            "created_at": metadata.get("created_at", ""),
            "updated_at": metadata.get("updated_at", ""),
            "created_by": metadata.get("created_by", ""),
        }

    async def delete_document(self, doc_id):
        self.collection.delete([doc_id])
        self._doc_index.pop(doc_id, None)

    async def mark_doc_updating(self, doc_id):
        pass

    async def count_documents(self):
        return len(self._doc_index)

    async def get_doc_content_hash(self, doc_id):
        return await self._redis.get(f"content_hash:{doc_id}")

    async def set_doc_content_hash(self, doc_id, c_hash):
        await self._redis.set(f"content_hash:{doc_id}", c_hash)


# ==================== Helpers Tests ====================

class TestHelpers:
    """Tests for pure utility functions in helpers.py."""

    def test_content_hash_consistency(self):
        """Same input must produce same hash."""
        h1 = content_hash("hello world")
        h2 = content_hash("hello world")
        assert h1 == h2
        assert len(h1) == 64  # SHA256 hex

    def test_content_hash_different(self):
        """Different inputs must produce different hashes."""
        assert content_hash("abc") != content_hash("xyz")

    def test_content_hash_empty(self):
        """Empty string must produce valid hash."""
        h = content_hash("")
        assert len(h) == 64

    def test_content_size_kb_empty(self):
        assert content_size_kb("") == "0.0KB"

    def test_content_size_kb_small(self):
        result = content_size_kb("hello")
        assert result.endswith("KB")

    def test_content_size_kb_large(self):
        text = "x" * 2048
        result = content_size_kb(text)
        assert "2.0KB" in result


# ==================== KnowledgeToolsReader Tests ====================

class TestKnowledgeToolsReader:
    """Tests for read-only knowledge base operations."""

    @pytest.fixture
    def reader(self):
        kb = MockKnowledgeBase()
        store = MockSourceStore()
        embedder = MockEmbedder()
        # Add a test document
        kb._doc_index["doc_1"] = {
            "doc_id": "doc_1", "title": "Test Doc",
            "path": "test/path", "tags": ["test", "doc"],
            "chunk_count": 1, "created_at": "2026-01-01", "updated_at": "2026-01-01",
            "created_by": "tester",
        }
        kb.collection.add(
            ids=["doc_1"],
            documents=["# Test Content"],
            metadatas=[{"path": "test/path", "source_path": "documents/test/path/doc_1/source.md",
                        "title": "Test Doc", "tags": "test,doc"}],
        )
        store.save_source("doc_1", "# Test Content", "test/path")
        return KnowledgeToolsReader(kb, embedder, store)

    @pytest.mark.asyncio
    async def test_search_knowledge_found(self, reader):
        result = await reader.search_knowledge("test", top_k=5)
        assert result["total"] > 0
        assert any("Test Doc" in r["title"] for r in result["results"])
        assert "retrieval_errors" in result
        assert {"channel", "raw_score", "final_score", "postprocess_reason"} <= set(result["results"][0])

    @pytest.mark.asyncio
    async def test_search_knowledge_reports_wait_and_retrieval_progress(self, reader):
        updates = []

        async def report(progress, message):
            updates.append((progress, message))

        result = await reader.search_knowledge(
            "test",
            top_k=5,
            progress_callback=report,
        )

        assert result["total"] > 0
        assert updates[0] == (5, "等待检索执行槽位")
        assert (15, "已获得执行槽位，检查查询缓存") in updates
        assert any(progress == 35 and "混合检索" in message for progress, message in updates)
        assert any(progress == 75 and "补充上下文" in message for progress, message in updates)
        assert updates[-1] == (90, "检索结果整理完成")

    @pytest.mark.asyncio
    async def test_search_knowledge_empty(self, reader):
        with pytest.raises(Exception):
            await reader.search_knowledge("", top_k=5)

    @pytest.mark.asyncio
    async def test_search_knowledge_filter_path(self, reader):
        result = await reader.search_knowledge("test", filter_path="test/path")
        assert result["total"] > 0

    @pytest.mark.asyncio
    async def test_search_knowledge_no_match(self, reader):
        result = await reader.search_knowledge("nonexistent", filter_path="other")
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_search_knowledge_returns_degraded_result_at_total_deadline(self, reader):
        object.__setattr__(reader.settings, "SEARCH_TOTAL_TIMEOUT_MS", 20)

        async def slow_search(_query):
            await asyncio.sleep(1)
            return []

        reader.retrieval_pipeline.search = slow_search

        result = await asyncio.wait_for(
            reader.search_knowledge("slow query"),
            timeout=0.2,
        )

        assert result["status"] == "degraded"
        assert result["timed_out"] is True
        assert result["results"] == []
        assert result["retrieval_errors"] == [
            {"channel": "request", "error": "timed out after 20ms"}
        ]
        assert result["timings_ms"]["total"] >= 20

    @pytest.mark.asyncio
    async def test_search_knowledge_reports_health_and_stage_timings(self, reader):
        result = await reader.search_knowledge("test", top_k=5)

        assert result["status"] == "ok"
        assert result["timed_out"] is False
        assert {"retrieval", "enrichment", "total"} <= set(result["timings_ms"])
        assert all(value >= 0 for value in result["timings_ms"].values())

    @pytest.mark.asyncio
    async def test_search_knowledge_keeps_hits_when_context_enrichment_times_out(self, reader):
        object.__setattr__(reader.settings, "SEARCH_TOTAL_TIMEOUT_MS", 200)
        object.__setattr__(reader.settings, "SEARCH_ENRICH_TIMEOUT_MS", 20)
        reader.retrieval_pipeline.search = AsyncMock(return_value=[{
            "doc_id": "doc_1",
            "chunk_index": 0,
            "content": "matched content",
            "title": "Test Doc",
            "path": "test/path",
        }])

        async def slow_enrichment(_items, **_kwargs):
            await asyncio.sleep(1)
            return []

        reader._enrich_search_results = slow_enrichment

        result = await reader.search_knowledge("test", top_k=1)

        assert result["status"] == "degraded"
        assert result["timed_out"] is True
        assert result["total"] == 1
        assert result["results"][0]["content"] == "matched content"
        assert result["results"][0]["citation"] == "test/path:Test Doc#chunk-0"
        assert result["retrieval_errors"] == [
            {"channel": "enrichment", "error": "timed out after 20ms"}
        ]

    @pytest.mark.asyncio
    async def test_search_knowledge_rejects_excess_work_instead_of_waiting_unbounded(self, reader):
        object.__setattr__(reader.settings, "SEARCH_QUEUE_TIMEOUT_MS", 10)
        reader._search_capacity = asyncio.Semaphore(1)
        started = asyncio.Event()
        release = asyncio.Event()

        async def held_search(_query):
            started.set()
            await release.wait()
            return []

        reader.retrieval_pipeline.search = held_search
        first = asyncio.create_task(reader.search_knowledge("first"))
        await started.wait()

        with pytest.raises(HTTPException) as exc_info:
            await reader.search_knowledge("second")

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["retry_after_ms"] == 10
        release.set()
        await first

    @pytest.mark.asyncio
    async def test_search_knowledge_prefetches_each_document_once(self, reader):
        reader.retrieval_pipeline.search = AsyncMock(return_value=[
            {
                "doc_id": "doc_1", "chunk_index": 0, "content": "first",
                "title": "Test Doc", "path": "test/path",
            },
            {
                "doc_id": "doc_1", "chunk_index": 1, "content": "second",
                "title": "Test Doc", "path": "test/path",
            },
        ])
        reader.kb.get_document_chunks = AsyncMock(return_value=[
            {"content": "first", "metadata": {"chunk_index": 0}},
            {"content": "second", "metadata": {"chunk_index": 1}},
        ])
        reader.kb._doc_index_get = AsyncMock(return_value={
            "updated_at": "2026-01-01", "tags": ["test"],
        })

        result = await reader.search_knowledge("test", top_k=2)

        assert result["total"] == 2
        reader.kb.get_document_chunks.assert_awaited_once_with("doc_1")
        reader.kb._doc_index_get.assert_awaited_once_with("doc_1")

    @pytest.mark.asyncio
    async def test_search_knowledge_limits_neighbor_context_to_requested_budget(self, reader):
        reader.retrieval_pipeline.search = AsyncMock(return_value=[{
            "doc_id": "doc_1",
            "chunk_index": 1,
            "content": "matched",
            "title": "Test Doc",
            "path": "test/path",
        }])
        reader.kb.get_document_chunks = AsyncMock(return_value=[
            {"content": "0123456789", "metadata": {"chunk_index": 0}},
            {"content": "matched", "metadata": {"chunk_index": 1}},
            {"content": "abcdefghij", "metadata": {"chunk_index": 2}},
        ])
        reader.kb._doc_index_get = AsyncMock(return_value={})

        result = await reader.search_knowledge(
            "test",
            top_k=1,
            max_context_chars=6,
        )

        hit = result["results"][0]
        assert hit["context_before"] == "789"
        assert hit["context_after"] == "abc"
        assert hit["context_truncated"] is True

    @pytest.mark.asyncio
    async def test_search_knowledge_can_skip_neighbor_context_reads(self, reader):
        reader.retrieval_pipeline.search = AsyncMock(return_value=[{
            "doc_id": "doc_1",
            "chunk_index": 0,
            "content": "matched",
            "title": "Test Doc",
            "path": "test/path",
        }])
        reader.kb.get_document_chunks = AsyncMock(
            side_effect=AssertionError("neighbor chunks should not be loaded")
        )
        reader.kb._doc_index_get = AsyncMock(return_value={})

        result = await reader.search_knowledge(
            "test",
            top_k=1,
            include_context=False,
        )

        hit = result["results"][0]
        assert hit["context_before"] == ""
        assert hit["context_after"] == ""
        reader.kb.get_document_chunks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_knowledge_does_not_rebuild_keyword_index(self, reader):
        reader.keyword_index.rebuild = AsyncMock(side_effect=AssertionError("unexpected rebuild"))

        result = await reader.search_knowledge("test", top_k=5)

        assert result["total"] > 0
        reader.keyword_index.rebuild.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_twenty_concurrent_searches_do_not_rebuild_keyword_index(self, reader):
        await reader.refresh_keyword_index()
        reader.keyword_index.rebuild = AsyncMock(side_effect=AssertionError("unexpected rebuild"))

        results = await asyncio.gather(*[
            reader.search_knowledge("test", top_k=5)
            for _ in range(20)
        ])

        assert len(results) == 20
        assert all(result["total"] > 0 for result in results)
        reader.keyword_index.rebuild.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_knowledge_uses_result_cache(self, reader):
        reader.redis = MockRedis()

        first = await reader.search_knowledge("test", top_k=5)
        reader.retrieval_pipeline.search = AsyncMock(side_effect=AssertionError("unexpected search"))
        second = await reader.search_knowledge("test", top_k=5)

        assert first["cache_hit"] is False
        assert second["cache_hit"] is True
        assert second["total"] == first["total"]
        reader.retrieval_pipeline.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_cache_invalidation_forces_new_search(self, reader):
        reader.redis = MockRedis()

        await reader.search_knowledge("test", top_k=5)
        await reader.invalidate_search_cache()
        reader.retrieval_pipeline.search = AsyncMock(return_value=[])

        result = await reader.search_knowledge("test", top_k=5)

        assert result["cache_hit"] is False
        assert result["total"] == 0
        reader.retrieval_pipeline.search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_list_documents_pagination(self, reader):
        result = await reader.list_documents(limit=10, offset=0)
        assert result["total"] > 0
        assert len(result["documents"]) > 0

    @pytest.mark.asyncio
    async def test_list_documents_empty(self, reader):
        reader.kb._doc_index = {}
        result = await reader.list_documents()
        assert result["total"] == 0
        assert result["documents"] == []

    @pytest.mark.asyncio
    async def test_list_directories(self, reader):
        result = await reader.list_directories()
        assert "tree" in result

    @pytest.mark.asyncio
    async def test_get_document_found(self, reader):
        result = await reader.get_document("doc_1")
        assert result["title"] == "Test Doc"
        assert result["doc_id"] == "doc_1"
        assert result["path"] == "test/path"

    @pytest.mark.asyncio
    async def test_get_document_not_found(self, reader):
        with pytest.raises(Exception):
            await reader.get_document("nonexistent")

    @pytest.mark.asyncio
    async def test_get_document_empty_id(self, reader):
        with pytest.raises(Exception):
            await reader.get_document("")


# ==================== KnowledgeTools Tests ====================

def _make_tools(fail_embedder=False):
    """Helper to create KnowledgeTools with mocked dependencies."""
    kb = MockKnowledgeBase()
    store = MockSourceStore()
    embedder = MockEmbedder(fail=fail_embedder)
    lock = MockWriteLock()
    auth = None
    tools = KnowledgeTools(kb, store, embedder, lock, auth)
    return tools, kb, store


class TestKnowledgeToolsAdd:
    """Tests for add_document and import_markdown."""

    @pytest.mark.asyncio
    async def test_add_document_basic(self):
        tools, kb, store = _make_tools()
        result = await tools.add_document(
            title="New Doc",
            content="# Hello World",
            path="test",
            tags=["tag1"],
        )
        assert result["success"] is True
        assert result["doc_id"] is not None
        assert result["task_id"] in tools.ingestion_tasks
        # Verify document exists in KB
        doc = await tools.get_document(result["doc_id"])
        assert doc["title"] == "New Doc"
        assert doc["path"] == "test"

    @pytest.mark.asyncio
    async def test_add_document_reports_ingestion_progress(self):
        tools, _kb, _store = _make_tools()
        updates = []

        async def report(progress, message):
            updates.append((progress, message))

        await tools.add_document(
            title="Progress",
            content="# Content",
            progress_callback=report,
        )

        assert updates[0] == (5, "解析 Markdown")
        assert (35, "生成向量") in updates
        assert (65, "等待写入锁") in updates
        assert updates[-1] == (100, "文档入库完成")
        assert [progress for progress, _message in updates] == sorted(
            progress for progress, _message in updates
        )

    @pytest.mark.asyncio
    async def test_add_document_lock_conflict_includes_retry_hint(self):
        class BusyWriteLock:
            async def __aenter__(self):
                raise WriteLockError("busy", retry_after_ms=750)

            async def __aexit__(self, *args):
                return None

        tools, _kb, _store = _make_tools()
        tools.write_lock = BusyWriteLock()

        with pytest.raises(HTTPException) as exc_info:
            await tools.add_document(title="Busy", content="# Content")

        assert exc_info.value.status_code == 423
        assert exc_info.value.detail == {
            "message": "知识库写入锁被占用，文档「Busy」暂时无法导入，请稍后重试",
            "retry_after_ms": 750,
        }

    @pytest.mark.asyncio
    async def test_add_document_updates_only_its_keyword_entries(self):
        tools, kb, store = _make_tools()
        tools.keyword_index.rebuild = AsyncMock(
            side_effect=AssertionError("unexpected full rebuild")
        )
        tools.keyword_index.upsert_document = AsyncMock()

        result = await tools.add_document(title="Incremental", content="# New term")

        tools.keyword_index.upsert_document.assert_awaited_once_with(kb, result["doc_id"])
        tools.keyword_index.rebuild.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_add_document_empty_title(self):
        tools, kb, store = _make_tools()
        with pytest.raises(Exception, match="标题不能为空"):
            await tools.add_document(title="", content="content")

    @pytest.mark.asyncio
    async def test_add_document_empty_content(self):
        tools, kb, store = _make_tools()
        with pytest.raises(Exception, match="内容不能为空"):
            await tools.add_document(title="Title", content="")

    @pytest.mark.asyncio
    async def test_add_document_default_path(self):
        tools, kb, store = _make_tools()
        result = await tools.add_document(title="Default Path", content="# Content")
        doc = await tools.get_document(result["doc_id"])
        assert doc["path"] == ""

    @pytest.mark.asyncio
    async def test_add_document_with_tags(self):
        tools, kb, store = _make_tools()
        result = await tools.add_document(
            title="Tagged", content="# Content",
            tags=["tech", "guide"],
        )
        doc = await tools.get_document(result["doc_id"])
        assert "tech" in doc["tags"]
        assert "guide" in doc["tags"]

    @pytest.mark.asyncio
    async def test_add_document_extracts_declared_header_tags_and_entities(self):
        tools, _, _ = _make_tools()
        result = await tools.add_document(
            title="Gateway",
            content="""# Gateway

Tags: deployment, internal
实体：MCP Gateway、Chroma
""",
            tags=["manual"],
        )

        doc = await tools.get_document(result["doc_id"])

        assert doc["tags"] == ["manual", "deployment", "internal"]
        assert doc["entities"] == ["MCP Gateway", "Chroma"]

    @pytest.mark.asyncio
    async def test_import_markdown_equivalent(self):
        """import_markdown should produce same result as add_document."""
        tools, kb, store = _make_tools()
        result = await tools.import_markdown(
            title="Imported", markdown_content="# Imported Content",
            path="imported",
        )
        assert result["success"] is True
        doc = await tools.get_document(result["doc_id"])
        assert doc["title"] == "Imported"
        assert doc["path"] == "imported"

    @pytest.mark.asyncio
    async def test_retry_failed_ingestion_task(self):
        tools, kb, store = _make_tools(fail_embedder=True)

        with pytest.raises(Exception):
            await tools.add_document(title="Retry Me", content="# Retry", path="retry")

        failed_task_id = next(
            task_id for task_id, task in tools.ingestion_tasks.items()
            if task["status"] == "failed"
        )
        failed_doc_id = tools.ingestion_tasks[failed_task_id]["doc_id"]

        tools.embedder.fail = False
        retry = await tools.retry_ingestion_task(failed_task_id, retried_by="admin")

        assert retry["success"] is True
        assert retry["retried_from"] == failed_task_id
        assert retry["doc_id"] == failed_doc_id
        assert tools.ingestion_tasks[failed_task_id]["retry_task_id"] == retry["task_id"]
        assert tools.ingestion_tasks[retry["task_id"]]["status"] == "succeeded"
        doc = await tools.get_document(retry["doc_id"])
        assert doc["title"] == "Retry Me"

    @pytest.mark.asyncio
    async def test_retry_rejects_non_failed_task(self):
        tools, kb, store = _make_tools()
        result = await tools.add_document(title="Done", content="# Done")

        with pytest.raises(Exception, match="只能重试失败任务"):
            await tools.retry_ingestion_task(result["task_id"])


class TestKnowledgeToolsUpdate:
    """Tests for update_document."""

    @pytest.mark.asyncio
    async def test_update_document_basic(self):
        tools, kb, store = _make_tools()
        # First add
        added = await tools.add_document(
            title="Original", content="# Original", path="test",
        )
        doc_id = added["doc_id"]
        # Then update
        result = await tools.update_document(
            doc_id=doc_id, title="Updated", content="# Updated",
            path="test", tags=["updated"],
        )
        assert result["success"] is True
        doc = await tools.get_document(doc_id)
        assert doc["title"] == "Updated"

    @pytest.mark.asyncio
    async def test_update_document_extracts_chinese_and_english_header_metadata(self):
        tools, _kb, _store = _make_tools()
        added = await tools.add_document(title="Gateway", content="# Gateway")

        await tools.update_document(
            doc_id=added["doc_id"],
            title="Gateway",
            content="""# Gateway

Tags: deployment, internal
实体：MCP Gateway、Chroma
""",
            tags=["manual"],
        )

        document = await tools.get_document(added["doc_id"])
        assert document["tags"] == ["manual", "deployment", "internal"]
        assert document["entities"] == ["MCP Gateway", "Chroma"]

    @pytest.mark.asyncio
    async def test_update_document_reports_long_running_stages(self):
        tools, _kb, _store = _make_tools()
        added = await tools.add_document(title="Original", content="# Original")
        updates = []

        async def report(progress, message):
            updates.append((progress, message))

        await tools.update_document(
            doc_id=added["doc_id"],
            title="Updated",
            content="# Updated",
            progress_callback=report,
        )

        assert updates[0] == (5, "读取现有文档")
        assert (35, "生成向量") in updates
        assert (65, "等待写入锁") in updates
        assert updates[-1] == (100, "文档更新完成")

    @pytest.mark.asyncio
    async def test_update_document_not_found(self):
        tools, kb, store = _make_tools()
        with pytest.raises(Exception, match="不存在"):
            await tools.update_document(
                doc_id="nonexistent", title="X", content="X",
            )

    @pytest.mark.asyncio
    async def test_update_document_skip_unchanged(self):
        tools, kb, store = _make_tools()
        added = await tools.add_document(
            title="Same", content="# Same", path="test", tags=["a"],
        )
        doc_id = added["doc_id"]
        # Same content update — should succeed (may either skip or re-save)
        result = await tools.update_document(
            doc_id=doc_id, title="Same", content="# Same",
            path="test", tags=["a"],
        )
        assert result["success"] is True
        # Change detection is a caching optimization; both paths are correct
        doc = await tools.get_document(doc_id)
        assert doc["title"] == "Same"

    @pytest.mark.asyncio
    async def test_update_document_empty_id(self):
        tools, kb, store = _make_tools()
        with pytest.raises(Exception, match="文档 ID 不能为空"):
            await tools.update_document(doc_id="", title="X", content="X")

    @pytest.mark.asyncio
    async def test_update_document_restores_old_chunks_when_rewrite_fails(self):
        tools, kb, store = _make_tools()
        added = await tools.add_document(
            title="Original", content="# Original", path="test", tags=["old"],
        )
        doc_id = added["doc_id"]

        kb.fail_next_add_chunks = True
        with pytest.raises(RuntimeError, match="simulated Chroma add failure"):
            await tools.update_document(
                doc_id=doc_id,
                title="Updated",
                content="# Updated",
                path="test",
                tags=["new"],
            )

        restored = await tools.get_document(doc_id)
        assert restored["title"] == "Original"
        assert "# Original" in restored["content"]
        assert "old" in restored["tags"]

    @pytest.mark.asyncio
    async def test_update_document_keeps_active_when_staging_fails(self):
        tools, kb, store = _make_tools()
        added = await tools.add_document(
            title="Original", content="# Original", path="test", tags=["old"],
        )
        doc_id = added["doc_id"]

        kb.collection.fail_next_add = True
        with pytest.raises(RuntimeError, match="simulated staging add failure"):
            await tools.update_document(
                doc_id=doc_id,
                title="Updated",
                content="# Updated",
                path="test",
                tags=["new"],
            )

        restored = await tools.get_document(doc_id)
        assert restored["title"] == "Original"
        assert "# Original" in restored["content"]
        assert all("__staging__" not in key for key in kb.collection._docs)

    @pytest.mark.asyncio
    async def test_update_document_records_cleanup_task_when_staging_source_cleanup_fails(self):
        tools, kb, store = _make_tools()
        added = await tools.add_document(
            title="Original", content="# Original", path="test", tags=["old"],
        )
        doc_id = added["doc_id"]

        store.fail_delete_source_path = True
        result = await tools.update_document(
            doc_id=doc_id,
            title="Updated",
            content="# Updated",
            path="test",
            tags=["new"],
        )

        assert result["success"] is True
        cleanup_task_id = next(iter(tools.cleanup_tasks))
        cleanup_result = tools.retry_cleanup_task(cleanup_task_id)
        assert cleanup_result["success"] is True
        assert tools.cleanup_tasks[cleanup_task_id]["status"] == "succeeded"


class TestKnowledgeToolsMetadata:
    """Metadata-only updates must stay independent from the source document."""

    @pytest.mark.asyncio
    async def test_metadata_update_preserves_source_and_survives_reindex(self):
        tools, kb, store = _make_tools()
        source = """# Gateway

标签：自动标签
核心实体：MCP Gateway、Chroma

正文内容不会被元数据编辑改写。
"""
        added = await tools.add_document(
            title="Gateway",
            content=source,
            path="docs",
            tags=["上传标签"],
        )
        doc_id = added["doc_id"]

        result = await tools.update_document_metadata(
            doc_id=doc_id,
            tags="人工标签；运维",
            entities=["MCP Gateway", "Redis"],
            updated_by="admin",
        )

        assert result["graph_rebuild_required"] is True
        assert store.get_source(doc_id, "docs") == source
        document = await tools.get_document(doc_id)
        assert document["tags"] == ["人工标签", "运维"]
        assert document["entities"] == ["MCP Gateway", "Redis"]

        metadata = (await kb.get_document_chunks(doc_id))[0]["metadata"]
        assert metadata["metadata_overridden"] is True
        assert metadata["tags_override"] == "人工标签,运维"
        assert metadata["entities_override"] == "MCP Gateway,Redis"

        await tools.reindex_document(doc_id)

        reindexed = await tools.get_document(doc_id)
        assert reindexed["tags"] == ["人工标签", "运维"]
        assert reindexed["entities"] == ["MCP Gateway", "Redis"]
        assert store.get_source(doc_id, "docs") == source

        from kb_graph import KnowledgeGraphBuilder

        graph_docs, _ = await kb.list_documents(limit=100, offset=0)
        builder = KnowledgeGraphBuilder.__new__(KnowledgeGraphBuilder)
        extraction = builder._build_extraction(
            [document.model_dump() for document in graph_docs],
            semantic_threshold=0.0,
        )
        entity_labels = {
            node["label"]
            for node in extraction["nodes"]
            if node.get("file_type") == "entity"
        }
        assert "Redis" in entity_labels
        assert "Chroma" not in entity_labels

    @pytest.mark.asyncio
    async def test_metadata_update_allows_empty_override_values(self):
        tools, _kb, _store = _make_tools()
        added = await tools.add_document(
            title="Gateway",
            content="# Gateway\n\n标签：自动标签\n核心实体：MCP Gateway",
        )

        await tools.update_document_metadata(
            doc_id=added["doc_id"],
            tags=[],
            entities=[],
        )
        await tools.reindex_document(added["doc_id"])

        document = await tools.get_document(added["doc_id"])
        assert document["tags"] == []
        assert document["entities"] == []


class TestKnowledgeToolsVersionsAndUpsert:
    @pytest.mark.asyncio
    async def test_update_creates_version_and_restore_rolls_back(self, tmp_path):
        tools, kb, store = _make_tools()
        tools.version_store = DocumentVersionStore(str(tmp_path))
        added = await tools.add_document(title="Versioned", content="# V1", path="docs", tags=["one"])

        await tools.update_document(
            doc_id=added["doc_id"],
            title="Versioned",
            content="# V2",
            path="docs",
            tags=["two"],
            updated_by="tester",
        )

        versions = await tools.list_document_versions(added["doc_id"])
        assert len(versions["versions"]) == 1
        assert versions["versions"][0]["reason"] == "before_update"

        restored = await tools.restore_document_version(
            added["doc_id"],
            versions["versions"][0]["version_id"],
            restored_by="tester",
        )
        assert restored["success"] is True
        doc = await tools.get_document(added["doc_id"])
        assert doc["content"].strip() == "# V1"
        assert doc["tags"] == ["one"]

    @pytest.mark.asyncio
    async def test_upsert_updates_existing_title_path_document(self, tmp_path):
        tools, kb, store = _make_tools()
        tools.version_store = DocumentVersionStore(str(tmp_path))
        added = await tools.add_document(title="Same", content="# Old", path="docs")

        result = await tools.upsert_document(
            title="Same",
            content="# New",
            path="docs",
            tags=["new"],
            match_strategy="title_path",
            on_conflict="update",
            created_by="tester",
        )

        assert result["action"] == "updated"
        assert result["doc_id"] == added["doc_id"]
        doc = await tools.get_document(added["doc_id"])
        assert doc["content"].strip() == "# New"
        assert doc["tags"] == ["new"]

    @pytest.mark.asyncio
    async def test_search_result_has_citation_and_context(self):
        tools, kb, store = _make_tools()
        added = await tools.add_document(title="Cited", content="# Heading\n\nBody text", path="docs")

        result = await tools.search_knowledge("Heading", top_k=1)

        assert result["results"]
        item = result["results"][0]
        assert item["doc_id"] == added["doc_id"]
        assert item["citation"] == "docs:Cited#chunk-0"
        assert "excerpt" in item
        assert "context_before" in item
        assert "context_after" in item


class TestKnowledgeToolsDelete:
    """Tests for delete_document."""

    @pytest.mark.asyncio
    async def test_delete_document_basic(self):
        tools, kb, store = _make_tools()
        added = await tools.add_document(
            title="To Delete", content="# Delete me",
        )
        doc_id = added["doc_id"]
        result = await tools.delete_document(doc_id)
        assert result["success"] is True
        # Should not be findable
        with pytest.raises(Exception):
            await tools.get_document(doc_id)

    @pytest.mark.asyncio
    async def test_delete_document_removes_only_its_keyword_entries(self):
        tools, kb, store = _make_tools()
        added = await tools.add_document(title="Remove Index", content="# Delete me")
        tools.keyword_index.rebuild = AsyncMock(
            side_effect=AssertionError("unexpected full rebuild")
        )
        tools.keyword_index.remove_document = MagicMock()

        await tools.delete_document(added["doc_id"])

        tools.keyword_index.remove_document.assert_called_once_with(added["doc_id"])
        tools.keyword_index.rebuild.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_document_not_found(self):
        tools, kb, store = _make_tools()
        with pytest.raises(Exception, match="不存在"):
            await tools.delete_document("nonexistent")

    @pytest.mark.asyncio
    async def test_delete_document_empty_id(self):
        tools, kb, store = _make_tools()
        with pytest.raises(Exception, match="文档 ID 不能为空"):
            await tools.delete_document("")


class TestKnowledgeToolsReindex:
    """Tests for reindex_document."""

    @pytest.mark.asyncio
    async def test_reindex_document_basic(self):
        tools, kb, store = _make_tools()
        added = await tools.add_document(
            title="To Reindex", content="# Original Content\n\nMore text here.",
            path="test",
        )
        doc_id = added["doc_id"]
        result = await tools.reindex_document(doc_id)
        assert result["success"] is True
        assert result["chunks_old"] > 0
        assert result["chunks_new"] > 0

    @pytest.mark.asyncio
    async def test_reindex_document_refreshes_header_metadata_and_keeps_manual_tags(self):
        tools, _kb, store = _make_tools()
        added = await tools.add_document(
            title="Gateway",
            content="""# Gateway

标签：旧标签
核心实体：Old Service
""",
            path="test",
            tags=["manual"],
        )
        doc_id = added["doc_id"]
        store.save_source(
            doc_id,
            """# Gateway

Tags: refreshed
Core Entities: MCP Gateway, Redis
""",
            "test",
        )

        await tools.reindex_document(doc_id)

        document = await tools.get_document(doc_id)
        assert document["tags"] == ["manual", "refreshed"]
        assert document["entities"] == ["MCP Gateway", "Redis"]

    @pytest.mark.asyncio
    async def test_reindex_document_reports_long_running_stages(self):
        tools, _kb, _store = _make_tools()
        added = await tools.add_document(title="To Reindex", content="# Original")
        updates = []

        async def report(progress, message):
            updates.append((progress, message))

        await tools.reindex_document(
            added["doc_id"],
            progress_callback=report,
        )

        assert updates[0] == (5, "读取现有文档")
        assert (35, "生成向量") in updates
        assert (65, "等待写入锁") in updates
        assert updates[-1] == (100, "文档索引重建完成")

    @pytest.mark.asyncio
    async def test_reindex_document_not_found(self):
        tools, kb, store = _make_tools()
        with pytest.raises(Exception, match="不存在"):
            await tools.reindex_document("nonexistent")

    @pytest.mark.asyncio
    async def test_reindex_document_restores_old_chunks_when_rewrite_fails(self):
        tools, kb, store = _make_tools()
        added = await tools.add_document(
            title="To Reindex", content="# Original Content\n\nMore text here.",
            path="test",
        )
        doc_id = added["doc_id"]

        kb.fail_next_add_chunks = True
        with pytest.raises(RuntimeError, match="simulated Chroma add failure"):
            await tools.reindex_document(doc_id)

        restored = await tools.get_document(doc_id)
        assert restored["title"] == "To Reindex"
        assert "# Original Content" in restored["content"]

    @pytest.mark.asyncio
    async def test_reindex_document_keeps_active_when_staging_fails(self):
        tools, kb, store = _make_tools()
        added = await tools.add_document(
            title="To Reindex", content="# Original Content\n\nMore text here.",
            path="test",
        )
        doc_id = added["doc_id"]

        kb.collection.fail_next_add = True
        with pytest.raises(RuntimeError, match="simulated staging add failure"):
            await tools.reindex_document(doc_id)

        restored = await tools.get_document(doc_id)
        assert restored["title"] == "To Reindex"
        assert "# Original Content" in restored["content"]
        assert all("__staging__" not in key for key in kb.collection._docs)


class TestKnowledgeToolsDirectory:
    """Tests for rename_directory and delete_directory."""

    @pytest.mark.asyncio
    async def test_rename_directory_basic(self):
        tools, kb, store = _make_tools()
        await tools.add_document(
            title="Doc1", content="# Doc1", path="old/path",
        )
        result = await tools.rename_directory("old/path", "new/path")
        assert result["success"] is True
        assert result["moved"] > 0

    @pytest.mark.asyncio
    async def test_rename_root_directory(self):
        tools, kb, store = _make_tools()
        with pytest.raises(Exception, match="不能重命名根目录"):
            await tools.rename_directory("", "new")

    @pytest.mark.asyncio
    async def test_rename_same_path(self):
        tools, kb, store = _make_tools()
        result = await tools.rename_directory("same/path", "same/path")
        assert result["moved"] == 0

    @pytest.mark.asyncio
    async def test_delete_directory_basic(self):
        tools, kb, store = _make_tools()
        await tools.add_document(
            title="Doc1", content="# Doc1", path="trash",
        )
        result = await tools.delete_directory("trash")
        assert result["success"] is True
        assert result["moved_to_root"] > 0

    @pytest.mark.asyncio
    async def test_delete_root_directory(self):
        tools, kb, store = _make_tools()
        with pytest.raises(Exception, match="不能删除根目录"):
            await tools.delete_directory("")

    @pytest.mark.asyncio
    async def test_delete_empty_directory(self):
        tools, kb, store = _make_tools()
        result = await tools.delete_directory("empty")
        assert result["success"] is True
        assert result["moved_to_root"] == 0


# ==================== Integration-like Edge Cases ====================

class TestKnowledgeToolsEdgeCases:
    """Edge case tests spanning multiple operations."""

    @pytest.mark.asyncio
    async def test_add_then_delete_then_add_same_title(self):
        tools, kb, store = _make_tools()
        r1 = await tools.add_document(title="Same", content="# V1", path="test")
        await tools.delete_document(r1["doc_id"])
        r2 = await tools.add_document(title="Same", content="# V2", path="test")
        doc = await tools.get_document(r2["doc_id"])
        assert doc["title"] == "Same"
        assert "# V2" in doc["content"]

    @pytest.mark.asyncio
    async def test_add_multiple_list(self):
        tools, kb, store = _make_tools()
        for i in range(3):
            await tools.add_document(
                title=f"Doc {i}", content=f"# Doc {i}", path=f"path{i}",
            )
        docs = await tools.list_documents()
        assert docs["total"] == 3

    @pytest.mark.asyncio
    async def test_search_after_add(self):
        tools, kb, store = _make_tools()
        await tools.add_document(title="Searchable", content="# Find me", tags=["important"])
        result = await tools.search_knowledge("find", top_k=5)
        assert result["total"] > 0

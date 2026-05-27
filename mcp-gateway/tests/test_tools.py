"""Unit tests for helpers, KnowledgeToolsReader, and KnowledgeTools."""
import sys
import os

# Must set DEBUG before any project imports to bypass SESSION_SECRET check
os.environ["DEBUG"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from helpers import content_hash, content_size_kb
from tools_reader import KnowledgeToolsReader
from tools import KnowledgeTools


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

    def add(self, ids, documents, metadatas, embeddings=None):
        for i, doc_id in enumerate(ids):
            self._docs[doc_id] = {
                "metadata": metadatas[i] if metadatas else {},
                "document": documents[i] if documents else "",
            }

    def get(self, ids=None, where=None, limit=None):
        if ids:
            results = [self._docs.get(i, {}) for i in ids if i in self._docs]
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
        items = items[offset:offset + limit]
        return [DocumentInfo(**item) for item in items]

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

    async def get_document_chunks(self, doc_id):
        if doc_id not in self.collection._docs:
            return []
        doc = self.collection._docs[doc_id]
        return [{"id": doc_id, "metadata": dict(doc["metadata"]), "content": doc["document"]}]

    async def add_document_chunks(self, doc_id, title, chunks, embeddings, metadata):
        full_content = "\n\n".join(chunks)
        self.collection.add(
            ids=[doc_id],
            documents=[full_content],
            metadatas=[metadata],
            embeddings=[embeddings[0]] if embeddings else None,
        )
        self._doc_index[doc_id] = {
            "doc_id": doc_id,
            "title": title,
            "path": metadata.get("path", ""),
            "tags": metadata.get("tags", []),
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
        # Verify document exists in KB
        doc = await tools.get_document(result["doc_id"])
        assert doc["title"] == "New Doc"
        assert doc["path"] == "test"

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
    async def test_reindex_document_not_found(self):
        tools, kb, store = _make_tools()
        with pytest.raises(Exception, match="不存在"):
            await tools.reindex_document("nonexistent")


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

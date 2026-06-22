import pytest

from src.consistency import KnowledgeBaseConsistencyChecker


class MockCollection:
    def __init__(self, metadatas):
        self.metadatas = metadatas

    def get(self, include=None):
        return {"metadatas": self.metadatas}


class MockKnowledgeBase:
    def __init__(self, docs, chunks_by_doc, chroma_metadatas):
        self.docs = docs
        self.chunks_by_doc = chunks_by_doc
        self.collection = MockCollection(chroma_metadatas)

    async def _doc_index_all(self):
        return self.docs

    async def get_document_chunks(self, doc_id):
        return self.chunks_by_doc.get(doc_id, [])


class MockSourceStore:
    def __init__(self, existing=None, full_paths=None):
        self.existing = set(existing or [])
        self.full_paths = set(full_paths or [])

    def source_exists(self, doc_id, path=""):
        return (doc_id, path) in self.existing

    def get_source_by_full_path(self, source_path):
        if source_path not in self.full_paths:
            raise FileNotFoundError(source_path)
        return "content"


def make_doc(doc_id="doc-1", chunk_count=1, path="docs"):
    return {
        "doc_id": doc_id,
        "title": doc_id,
        "path": path,
        "chunk_count": chunk_count,
    }


def make_chunk(doc_id="doc-1", source_path=None):
    metadata = {"doc_id": doc_id}
    if source_path:
        metadata["source_path"] = source_path
    return {"content": "chunk", "metadata": metadata}


@pytest.mark.asyncio
async def test_consistency_check_passes_for_aligned_storage():
    kb = MockKnowledgeBase(
        docs=[make_doc()],
        chunks_by_doc={"doc-1": [make_chunk(source_path="/kb/doc-1.md")]},
        chroma_metadatas=[{"doc_id": "doc-1"}],
    )
    checker = KnowledgeBaseConsistencyChecker(kb, MockSourceStore(full_paths={"/kb/doc-1.md"}))

    result = await checker.check()

    assert result["success"] is True
    assert result["issue_count"] == 0
    assert result["stats"]["indexed_documents"] == 1
    assert result["stats"]["chroma_documents"] == 1


@pytest.mark.asyncio
async def test_consistency_check_reports_missing_chunks_and_source():
    kb = MockKnowledgeBase(
        docs=[make_doc()],
        chunks_by_doc={},
        chroma_metadatas=[],
    )
    checker = KnowledgeBaseConsistencyChecker(kb, MockSourceStore())

    result = await checker.check()

    assert result["success"] is False
    assert {issue["code"] for issue in result["issues"]} == {"missing_chunks", "missing_source"}
    assert result["stats"]["errors"] == 2


@pytest.mark.asyncio
async def test_consistency_check_reports_chunk_count_mismatch():
    kb = MockKnowledgeBase(
        docs=[make_doc(chunk_count=2)],
        chunks_by_doc={"doc-1": [make_chunk()]},
        chroma_metadatas=[{"doc_id": "doc-1"}],
    )
    checker = KnowledgeBaseConsistencyChecker(kb, MockSourceStore(existing={("doc-1", "docs")}))

    result = await checker.check()

    assert result["success"] is True
    assert result["issues"][0]["code"] == "chunk_count_mismatch"
    assert result["issues"][0]["severity"] == "warning"
    assert result["issues"][0]["details"] == {"indexed": 2, "actual": 1}


@pytest.mark.asyncio
async def test_consistency_check_reports_orphan_chroma_document():
    kb = MockKnowledgeBase(
        docs=[make_doc()],
        chunks_by_doc={"doc-1": [make_chunk()]},
        chroma_metadatas=[{"doc_id": "doc-1"}, {"doc_id": "orphan"}],
    )
    checker = KnowledgeBaseConsistencyChecker(kb, MockSourceStore(existing={("doc-1", "docs")}))

    result = await checker.check()

    orphan = [issue for issue in result["issues"] if issue["code"] == "orphan_chroma_document"]
    assert result["success"] is True
    assert orphan[0]["doc_id"] == "orphan"


@pytest.mark.asyncio
async def test_consistency_check_reports_invalid_chroma_metadata():
    kb = MockKnowledgeBase(
        docs=[],
        chunks_by_doc={},
        chroma_metadatas=[None, {"title": "missing doc id"}],
    )
    checker = KnowledgeBaseConsistencyChecker(kb, MockSourceStore())

    result = await checker.check()

    assert result["success"] is True
    assert [issue["code"] for issue in result["issues"]] == [
        "invalid_chroma_metadata",
        "missing_chroma_doc_id",
    ]

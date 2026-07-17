import pytest
from fastapi import HTTPException

from src.ingestion import DocumentIngestionPipeline


class MockLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class MockKB:
    def __init__(self):
        self.docs = {}
        self.hashes = {}
        self.fail_add = False

    async def add_document_chunks(self, doc_id, title, chunks, embeddings, metadata):
        if self.fail_add:
            raise RuntimeError("vector write failed")
        self.docs[doc_id] = {
            "title": title,
            "chunks": chunks,
            "embeddings": embeddings,
            "metadata": metadata,
        }

    async def set_doc_content_hash(self, doc_id, value):
        self.hashes[doc_id] = value


class MockSourceStore:
    def __init__(self):
        self.sources = {}
        self.deleted = []

    def save_source(self, doc_id, content, path=""):
        source_path = f"documents/{path}/{doc_id}/source.md"
        self.sources[source_path] = content
        return source_path

    def delete_source_by_path(self, source_path):
        self.deleted.append(source_path)
        self.sources.pop(source_path, None)


class MockEmbedder:
    def __init__(self, fail=False):
        self.fail = fail

    async def embed(self, chunks):
        if self.fail:
            return []
        return [[0.1] for _ in chunks]


def make_pipeline(kb=None, store=None, embedder=None):
    return DocumentIngestionPipeline(
        kb=kb or MockKB(),
        source_store=store or MockSourceStore(),
        embedder=embedder or MockEmbedder(),
        write_lock=MockLock(),
        chunk_size=128,
        chunk_overlap=10,
    )


@pytest.mark.asyncio
async def test_ingestion_pipeline_records_node_logs():
    kb = MockKB()
    pipeline = make_pipeline(kb=kb)

    result = await pipeline.import_document(
        title="Doc",
        content="# Title\n\nBody",
        path="guides",
        tags=["tag"],
        created_by="tester",
        doc_id="doc-1",
    )

    assert result.doc_id == "doc-1"
    assert result.task.status == "succeeded"
    assert [node.node for node in result.task.nodes] == [
        "parse_markdown",
        "normalize_content",
        "chunk",
        "embedding",
        "persist_source",
        "persist_vector",
        "commit_index",
    ]
    assert kb.docs["doc-1"]["metadata"]["source_path"] == result.source_path
    assert kb.hashes["doc-1"]


@pytest.mark.asyncio
async def test_ingestion_pipeline_extracts_header_tags_and_entities_without_model():
    kb = MockKB()
    pipeline = make_pipeline(kb=kb)

    await pipeline.import_document(
        title="Gateway",
        content="""# Gateway

Tags: deployment, internal
核心实体：MCP Gateway、Chroma

## Details
实体：正文不应解析
""",
        path="guides",
        tags=["manual"],
        doc_id="doc-metadata",
    )

    metadata = kb.docs["doc-metadata"]["metadata"]
    assert metadata["tags"] == ["manual", "deployment", "internal"]
    assert metadata["header_tags"] == ["deployment", "internal"]
    assert metadata["entities"] == ["MCP Gateway", "Chroma"]


@pytest.mark.asyncio
async def test_ingestion_pipeline_reports_embedding_failure():
    pipeline = make_pipeline(embedder=MockEmbedder(fail=True))

    with pytest.raises(HTTPException) as exc:
        await pipeline.import_document(
            title="Doc",
            content="# Title",
            path="",
            tags=[],
            doc_id="doc-1",
        )

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_ingestion_pipeline_cleans_source_after_vector_failure():
    kb = MockKB()
    kb.fail_add = True
    store = MockSourceStore()
    pipeline = make_pipeline(kb=kb, store=store)

    with pytest.raises(RuntimeError):
        await pipeline.import_document(
            title="Doc",
            content="# Title",
            path="docs",
            tags=[],
            doc_id="doc-1",
        )

    assert store.deleted == ["documents/docs/doc-1/source.md"]
    assert store.sources == {}

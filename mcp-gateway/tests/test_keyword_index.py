import pytest

from src.rag.keyword_index import KeywordInvertedIndex
from src.rag.retrieval import KeywordChannel, RetrievalQuery


class MockKB:
    def __init__(self):
        self.docs = [
            {"doc_id": "doc-1", "title": "Alpha Manual", "path": "guides", "tags": ["ops"], "chunk_count": 1},
            {"doc_id": "doc-2", "title": "Beta Manual", "path": "refs", "tags": ["dev"], "chunk_count": 1},
        ]
        self.chunks = {
            "doc-1": [
                {
                    "content": "alpha deployment steps",
                    "metadata": {"doc_id": "doc-1", "title": "Alpha Manual", "path": "guides", "chunk_index": 0, "total_chunks": 1},
                }
            ],
            "doc-2": [
                {
                    "content": "beta api reference",
                    "metadata": {"doc_id": "doc-2", "title": "Beta Manual", "path": "refs", "chunk_index": 0, "total_chunks": 1},
                }
            ],
        }

    async def _doc_index_all(self):
        return self.docs

    async def _doc_index_get(self, doc_id):
        return next((doc for doc in self.docs if doc["doc_id"] == doc_id), None)

    async def get_document_chunks(self, doc_id):
        return self.chunks.get(doc_id, [])


@pytest.mark.asyncio
async def test_keyword_inverted_index_searches_exact_terms_and_filters():
    index = KeywordInvertedIndex()
    await index.rebuild(MockKB())

    results = index.search(RetrievalQuery(text="alpha", filter_tags=["ops"], top_k=5), top_k=5)

    assert len(results) == 1
    assert results[0].result.doc_id == "doc-1"
    assert results[0].channel == "keyword"


@pytest.mark.asyncio
async def test_keyword_inverted_index_respects_path_filter():
    index = KeywordInvertedIndex()
    await index.rebuild(MockKB())

    results = index.search(RetrievalQuery(text="manual", filter_path="refs", top_k=5), top_k=5)

    assert len(results) == 1
    assert results[0].result.doc_id == "doc-2"


@pytest.mark.asyncio
async def test_keyword_inverted_index_recalls_overlapping_chinese_phrases():
    kb = MockKB()
    kb.docs[0] = {
        **kb.docs[0],
        "title": "内部知识库管理平台",
    }
    kb.chunks["doc-1"][0]["content"] = "通过 MCP 检索内部知识库管理平台"
    index = KeywordInvertedIndex()
    await index.rebuild(kb)

    results = index.search(RetrievalQuery(text="知识库检索", top_k=5), top_k=5)

    assert [candidate.result.doc_id for candidate in results] == ["doc-1"]


@pytest.mark.asyncio
async def test_keyword_inverted_index_incrementally_replaces_and_removes_documents():
    kb = MockKB()
    index = KeywordInvertedIndex()
    await index.rebuild(kb)
    kb.docs[0] = {**kb.docs[0], "title": "Gamma Manual"}
    kb.chunks["doc-1"] = [
        {
            "content": "gamma rollout steps",
            "metadata": {
                "doc_id": "doc-1", "title": "Gamma Manual", "path": "guides",
                "chunk_index": 0, "total_chunks": 1,
            },
        }
    ]

    await index.upsert_document(kb, "doc-1")

    assert index.search(RetrievalQuery(text="alpha"), top_k=5) == []
    updated = index.search(RetrievalQuery(text="gamma"), top_k=5)
    assert [candidate.result.doc_id for candidate in updated] == ["doc-1"]

    index.remove_document("doc-2")

    assert index.search(RetrievalQuery(text="beta"), top_k=5) == []


@pytest.mark.asyncio
async def test_ready_keyword_index_zero_hit_does_not_scan_knowledge_base():
    class CountingKB(MockKB):
        def __init__(self):
            super().__init__()
            self.catalog_reads = 0

        async def _doc_index_all(self):
            self.catalog_reads += 1
            return await super()._doc_index_all()

    kb = CountingKB()
    index = KeywordInvertedIndex()
    await index.rebuild(kb)
    kb.catalog_reads = 0

    results = await KeywordChannel(kb, index).search(
        RetrievalQuery(text="term-that-does-not-exist", top_k=5)
    )

    assert results == []
    assert kb.catalog_reads == 0

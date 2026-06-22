import pytest

from src.rag.keyword_index import KeywordInvertedIndex
from src.rag.retrieval import RetrievalQuery


class MockKB:
    async def _doc_index_all(self):
        return [
            {"doc_id": "doc-1", "title": "Alpha Manual", "path": "guides", "tags": ["ops"], "chunk_count": 1},
            {"doc_id": "doc-2", "title": "Beta Manual", "path": "refs", "tags": ["dev"], "chunk_count": 1},
        ]

    async def get_document_chunks(self, doc_id):
        chunks = {
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
        return chunks.get(doc_id, [])


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

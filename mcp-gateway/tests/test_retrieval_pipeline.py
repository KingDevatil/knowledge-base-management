import asyncio

import pytest

from src.models import SearchResult
from src.rag.retrieval import (
    KeywordChannel,
    RetrievalCandidate,
    RetrievalPipeline,
    RetrievalQuery,
    SearchResultPostProcessor,
    StructureChannel,
    VectorChannel,
    normalize_query_text,
    tokenize_query,
)


class MockKnowledgeBase:
    def __init__(self):
        self.docs = [
            {"doc_id": "doc-1", "title": "Alpha Manual", "path": "guides", "tags": ["ops"], "chunk_count": 2},
            {"doc_id": "doc-2", "title": "Beta Reference", "path": "refs", "tags": "dev,api", "chunk_count": 1},
        ]
        self.chunks = {
            "doc-1": [
                {
                    "content": "alpha install guide alpha",
                    "metadata": {"doc_id": "doc-1", "title": "Alpha Manual", "path": "guides", "chunk_index": 0, "total_chunks": 2},
                },
                {
                    "content": "operations checklist",
                    "metadata": {"doc_id": "doc-1", "title": "Alpha Manual", "path": "guides", "chunk_index": 1, "total_chunks": 2},
                },
            ],
            "doc-2": [
                {
                    "content": "beta api reference",
                    "metadata": {"doc_id": "doc-2", "title": "Beta Reference", "path": "refs", "chunk_index": 0, "total_chunks": 1},
                },
            ],
        }

    async def _doc_index_all(self):
        return self.docs

    async def get_document_chunks(self, doc_id):
        return self.chunks.get(doc_id, [])

    async def search(self, query_embedding, top_k=5, filter_tags=None, filter_path=""):
        return [
            SearchResult(
                content="vector alpha",
                title="Alpha Manual",
                path="guides",
                doc_id="doc-1",
                chunk_index=0,
                total_chunks=2,
                score=0.9,
            )
        ][:top_k]


class MockEmbedder:
    async def embed_single(self, query):
        return [0.1, 0.2]


def test_tokenize_query_normalizes_common_separators():
    assert tokenize_query(" Alpha，beta; gamma ") == ["alpha", "beta", "gamma"]


def test_normalize_query_text_collapses_whitespace_and_deduplicates_terms():
    assert normalize_query_text(" Alpha，alpha   beta  ") == "alpha beta"


@pytest.mark.asyncio
async def test_keyword_channel_scores_title_and_content_matches():
    channel = KeywordChannel(MockKnowledgeBase())

    results = await channel.search(RetrievalQuery(text="alpha", top_k=5))

    assert results[0].channel == "keyword"
    assert results[0].result.doc_id == "doc-1"
    assert results[0].raw_score > 0


@pytest.mark.asyncio
async def test_structure_channel_matches_doc_id_and_filters():
    channel = StructureChannel(MockKnowledgeBase())

    results = await channel.search(RetrievalQuery(text="doc-2", filter_tags=["api"], top_k=5))

    assert len(results) == 1
    assert results[0].result.doc_id == "doc-2"
    assert results[0].channel == "structure"


@pytest.mark.asyncio
async def test_vector_channel_adapts_existing_kb_search():
    channel = VectorChannel(MockKnowledgeBase(), MockEmbedder())

    results = await channel.search(RetrievalQuery(text="alpha", top_k=5))

    assert results[0].channel == "vector"
    assert results[0].raw_score == 0.9


def test_postprocessor_deduplicates_same_chunk_and_keeps_best_score():
    result = SearchResult(content="a", title="A", doc_id="doc-1", chunk_index=0, score=0.1)
    candidates = [
        RetrievalCandidate(result=result, channel="keyword", raw_score=1.0),
        RetrievalCandidate(result=result, channel="structure", raw_score=9.0),
    ]

    processed = SearchResultPostProcessor().process(candidates, RetrievalQuery(text="a", top_k=5))

    assert len(processed) == 1
    assert processed[0].channel == "structure"
    assert processed[0].final_score >= 0.9


@pytest.mark.asyncio
async def test_pipeline_returns_debug_fields():
    pipeline = RetrievalPipeline(
        channels=[
            KeywordChannel(MockKnowledgeBase()),
            StructureChannel(MockKnowledgeBase()),
        ]
    )

    results = await pipeline.search(RetrievalQuery(text="alpha", top_k=2))

    assert results
    assert {"channel", "raw_score", "final_score", "postprocess_reason"} <= set(results[0])
    assert len(results) <= 2


@pytest.mark.asyncio
async def test_pipeline_starts_enabled_channels_concurrently():
    peer_started = asyncio.Event()

    class WaitForPeerChannel:
        name = "wait-for-peer"
        enabled = True
        timeout_ms = 100

        async def search(self, query):
            await asyncio.wait_for(peer_started.wait(), timeout=0.05)
            return []

    class SignalPeerChannel:
        name = "signal-peer"
        enabled = True
        timeout_ms = 100

        async def search(self, query):
            peer_started.set()
            return []

    pipeline = RetrievalPipeline(channels=[WaitForPeerChannel(), SignalPeerChannel()])

    await pipeline.search(RetrievalQuery(text="alpha"))

    assert pipeline.last_errors == []


@pytest.mark.asyncio
async def test_pipeline_times_out_slow_channel_without_losing_fast_results():
    class SlowChannel:
        name = "slow"
        enabled = True
        timeout_ms = 10

        async def search(self, query):
            await asyncio.sleep(0.05)
            return []

    class FastChannel:
        name = "fast"
        enabled = True
        timeout_ms = 100

        async def search(self, query):
            return [
                RetrievalCandidate(
                    result=SearchResult(
                        content="fast result",
                        title="Fast",
                        doc_id="doc-fast",
                        chunk_index=0,
                        score=0.8,
                    ),
                    channel=self.name,
                    raw_score=0.8,
                )
            ]

    pipeline = RetrievalPipeline(channels=[SlowChannel(), FastChannel()])

    results = await pipeline.search(RetrievalQuery(text="fast"))

    assert [result["channel"] for result in results] == ["fast"]
    assert pipeline.last_errors == [
        {"channel": "slow", "error": "timed out after 10ms"}
    ]


@pytest.mark.asyncio
async def test_pipeline_keeps_channel_errors_isolated_per_concurrent_request():
    class QueryAwareChannel:
        name = "query-aware"
        enabled = True
        timeout_ms = 100

        async def search(self, query):
            if query.text == "bad":
                await asyncio.sleep(0.01)
                raise RuntimeError("bad query")
            await asyncio.sleep(0.02)
            return []

    pipeline = RetrievalPipeline(channels=[QueryAwareChannel()])

    async def run(query):
        await pipeline.search(RetrievalQuery(text=query))
        return pipeline.last_errors

    bad_errors, good_errors = await asyncio.gather(run("bad"), run("good"))

    assert bad_errors == [{"channel": "query-aware", "error": "bad query"}]
    assert good_errors == []


@pytest.mark.asyncio
async def test_pipeline_expands_neighbor_chunks_when_configured():
    class SingleHitChannel:
        name = "single"
        enabled = True
        timeout_ms = 100

        async def search(self, query):
            return [
                RetrievalCandidate(
                    result=SearchResult(
                        content="hit",
                        title="Alpha Manual",
                        path="guides",
                        doc_id="doc-1",
                        chunk_index=0,
                        total_chunks=2,
                        score=0.8,
                    ),
                    channel="single",
                    raw_score=0.8,
                )
            ]

    pipeline = RetrievalPipeline(
        channels=[SingleHitChannel()],
        kb=MockKnowledgeBase(),
        neighbor_window=1,
    )

    results = await pipeline.search(RetrievalQuery(text="alpha", top_k=3))

    neighbor = [result for result in results if result["channel"] == "single+neighbor"]
    assert neighbor
    assert neighbor[0]["chunk_index"] == 1
    assert "channel=single+neighbor; normalized" in neighbor[0]["postprocess_reason"]


@pytest.mark.asyncio
async def test_pipeline_keeps_channel_hits_when_neighbor_expansion_times_out():
    class FastChannel:
        name = "fast"
        enabled = True
        timeout_ms = 100

        async def search(self, query):
            return [
                RetrievalCandidate(
                    result=SearchResult(
                        content="hit",
                        title="Alpha Manual",
                        doc_id="doc-1",
                        chunk_index=0,
                        score=0.8,
                    ),
                    channel=self.name,
                    raw_score=0.8,
                )
            ]

    class SlowNeighborKB:
        async def get_document_chunks(self, doc_id):
            await asyncio.sleep(1)
            return []

    pipeline = RetrievalPipeline(
        channels=[FastChannel()],
        kb=SlowNeighborKB(),
        neighbor_window=1,
        neighbor_timeout_ms=10,
    )

    results = await pipeline.search(RetrievalQuery(text="alpha", top_k=3))

    assert [result["channel"] for result in results] == ["fast"]
    assert pipeline.last_errors == [
        {"channel": "neighbor_expansion", "error": "timed out after 10ms"}
    ]

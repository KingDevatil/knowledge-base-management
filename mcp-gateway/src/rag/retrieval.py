"""Composable retrieval pipeline for multi-channel RAG search."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from models import SearchResult


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    top_k: int = 5
    filter_tags: list[str] = field(default_factory=list)
    filter_path: str = ""
    original_text: str = ""


@dataclass
class RetrievalCandidate:
    result: SearchResult
    channel: str
    raw_score: float
    final_score: float = 0.0
    postprocess_reason: str = ""


class RetrievalChannel(Protocol):
    name: str
    enabled: bool
    timeout_ms: int

    async def search(self, query: RetrievalQuery) -> list[RetrievalCandidate]:
        ...


class VectorChannel:
    name = "vector"
    enabled = True
    timeout_ms = 5000

    def __init__(self, kb: Any, embedder: Any):
        self.kb = kb
        self.embedder = embedder

    async def search(self, query: RetrievalQuery) -> list[RetrievalCandidate]:
        embedding = await self.embedder.embed_single(query.text)
        if not embedding:
            return []
        results = await self.kb.search(
            query_embedding=embedding,
            top_k=query.top_k,
            filter_tags=query.filter_tags,
            filter_path=query.filter_path,
        )
        return [
            RetrievalCandidate(result=result, channel=self.name, raw_score=result.score)
            for result in results
        ]


class KeywordChannel:
    name = "keyword"
    enabled = True
    timeout_ms = 3000

    def __init__(self, kb: Any, keyword_index: Any | None = None):
        self.kb = kb
        self.keyword_index = keyword_index

    async def search(self, query: RetrievalQuery) -> list[RetrievalCandidate]:
        terms = tokenize_query(query.text)
        if not terms:
            return []

        if self.keyword_index is not None:
            indexed = self.keyword_index.search(query, query.top_k)
            if indexed:
                return indexed

        candidates: list[RetrievalCandidate] = []
        for doc in await self.kb._doc_index_all():
            if not doc_matches_filters(doc, query):
                continue
            chunks = await self.kb.get_document_chunks(doc.get("doc_id", ""))
            for chunk in chunks:
                score = keyword_score(terms, doc, chunk.get("content", ""))
                if score <= 0:
                    continue
                candidates.append(
                    RetrievalCandidate(
                        result=chunk_to_search_result(doc, chunk, score),
                        channel=self.name,
                        raw_score=score,
                    )
                )
        return sorted(candidates, key=lambda item: item.raw_score, reverse=True)[: query.top_k]


class StructureChannel:
    name = "structure"
    enabled = True
    timeout_ms = 2000

    def __init__(self, kb: Any):
        self.kb = kb

    async def search(self, query: RetrievalQuery) -> list[RetrievalCandidate]:
        query_text = query.text.strip().lower()
        if not query_text and not query.filter_path and not query.filter_tags:
            return []

        candidates: list[RetrievalCandidate] = []
        for doc in await self.kb._doc_index_all():
            if not doc_matches_filters(doc, query):
                continue
            score = structure_score(query_text, doc)
            if score <= 0:
                continue
            chunks = await self.kb.get_document_chunks(doc.get("doc_id", ""))
            if not chunks:
                continue
            candidates.append(
                RetrievalCandidate(
                    result=chunk_to_search_result(doc, chunks[0], score),
                    channel=self.name,
                    raw_score=score,
                )
            )
        return sorted(candidates, key=lambda item: item.raw_score, reverse=True)[: query.top_k]


class SearchResultPostProcessor:
    def process(
        self,
        candidates: list[RetrievalCandidate],
        query: RetrievalQuery,
    ) -> list[RetrievalCandidate]:
        best_by_chunk: dict[tuple[str, int], RetrievalCandidate] = {}
        for candidate in candidates:
            key = (candidate.result.doc_id, candidate.result.chunk_index)
            rerank_boost = rerank_score(candidate, query)
            candidate.final_score = min(normalized_score(candidate) + rerank_boost, 1.0)
            candidate.postprocess_reason = (
                f"channel={candidate.channel}; normalized; rerank={round(rerank_boost, 4)}"
            )
            current = best_by_chunk.get(key)
            if current is None or candidate.final_score > current.final_score:
                best_by_chunk[key] = candidate

        return sorted(
            best_by_chunk.values(),
            key=lambda item: item.final_score,
            reverse=True,
        )[: query.top_k]


class RetrievalPipeline:
    def __init__(
        self,
        channels: list[RetrievalChannel],
        postprocessor: SearchResultPostProcessor | None = None,
        kb: Any | None = None,
        neighbor_window: int = 0,
    ):
        self.channels = channels
        self.postprocessor = postprocessor or SearchResultPostProcessor()
        self.kb = kb
        self.neighbor_window = max(0, neighbor_window)
        self.last_errors: list[dict[str, str]] = []

    async def search(self, query: RetrievalQuery) -> list[dict[str, Any]]:
        query = rewrite_query(query)
        candidates: list[RetrievalCandidate] = []
        self.last_errors = []
        for channel in self.channels:
            if not channel.enabled:
                continue
            try:
                candidates.extend(await channel.search(query))
            except Exception as exc:
                self.last_errors.append({
                    "channel": channel.name,
                    "error": str(exc),
                })

        if self.kb and self.neighbor_window > 0:
            candidates.extend(await self._expand_neighbors(candidates))

        processed = self.postprocessor.process(candidates, query)
        return [candidate_to_debug_dict(candidate) for candidate in processed]

    async def _expand_neighbors(self, candidates: list[RetrievalCandidate]) -> list[RetrievalCandidate]:
        expanded: list[RetrievalCandidate] = []
        chunks_by_doc: dict[str, list[dict[str, Any]]] = {}
        existing = {(candidate.result.doc_id, candidate.result.chunk_index) for candidate in candidates}

        for candidate in candidates:
            doc_id = candidate.result.doc_id
            if not doc_id:
                continue
            if doc_id not in chunks_by_doc:
                chunks_by_doc[doc_id] = await self.kb.get_document_chunks(doc_id)
            chunks = chunks_by_doc[doc_id]
            by_index = {
                int((chunk.get("metadata") or {}).get("chunk_index", 0)): chunk
                for chunk in chunks
            }

            for offset in range(-self.neighbor_window, self.neighbor_window + 1):
                if offset == 0:
                    continue
                neighbor_index = candidate.result.chunk_index + offset
                key = (doc_id, neighbor_index)
                if key in existing or neighbor_index not in by_index:
                    continue
                existing.add(key)
                doc = {
                    "doc_id": doc_id,
                    "title": candidate.result.title,
                    "path": candidate.result.path,
                    "chunk_count": candidate.result.total_chunks,
                }
                expanded.append(
                    RetrievalCandidate(
                        result=chunk_to_search_result(
                            doc,
                            by_index[neighbor_index],
                            candidate.raw_score * 0.85,
                        ),
                        channel=f"{candidate.channel}+neighbor",
                        raw_score=candidate.raw_score * 0.85,
                    )
                )
        return expanded


def tokenize_query(query: str) -> list[str]:
    return [
        token.lower()
        for token in re.split(r"\s+|[,，。；;：:、]+", query.strip())
        if token.strip()
    ]


def rewrite_query(query: RetrievalQuery) -> RetrievalQuery:
    text = normalize_query_text(query.text)
    return RetrievalQuery(
        text=text,
        top_k=query.top_k,
        filter_tags=query.filter_tags,
        filter_path=query.filter_path,
        original_text=query.original_text or query.text,
    )


def normalize_query_text(query: str) -> str:
    tokens = tokenize_query(" ".join((query or "").strip().split()))
    deduped: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return " ".join(deduped)


def doc_matches_filters(doc: dict[str, Any], query: RetrievalQuery) -> bool:
    if query.filter_path and doc.get("path", "") != query.filter_path:
        return False
    if query.filter_tags:
        doc_tags = normalize_tags(doc.get("tags", []))
        if not set(query.filter_tags) & set(doc_tags):
            return False
    return True


def normalize_tags(tags: Any) -> list[str]:
    if isinstance(tags, list):
        return [str(tag).strip() for tag in tags if str(tag).strip()]
    if isinstance(tags, str):
        return [tag.strip() for tag in tags.replace("，", ",").split(",") if tag.strip()]
    return []


def keyword_score(terms: list[str], doc: dict[str, Any], content: str) -> float:
    title = str(doc.get("title", "")).lower()
    path = str(doc.get("path", "")).lower()
    haystack = content.lower()
    score = 0.0
    for term in terms:
        if term in title:
            score += 3.0
        if term in path:
            score += 1.5
        score += min(haystack.count(term), 5) * 1.0
    return score


def structure_score(query_text: str, doc: dict[str, Any]) -> float:
    doc_id = str(doc.get("doc_id", "")).lower()
    title = str(doc.get("title", "")).lower()
    path = str(doc.get("path", "")).lower()
    if query_text == doc_id:
        return 10.0
    if query_text and query_text == title:
        return 8.0
    if query_text and query_text in title:
        return 5.0
    if query_text and query_text in path:
        return 3.0
    return 1.0 if not query_text else 0.0


def normalized_score(candidate: RetrievalCandidate) -> float:
    if candidate.channel == "vector":
        return candidate.raw_score
    if candidate.channel == "structure":
        return min(candidate.raw_score / 10.0, 1.0)
    return min(candidate.raw_score / 10.0, 1.0)


def rerank_score(candidate: RetrievalCandidate, query: RetrievalQuery) -> float:
    terms = tokenize_query(query.text)
    if not terms:
        return 0.0
    title = candidate.result.title.lower()
    path = candidate.result.path.lower()
    content = candidate.result.content.lower()
    boost = 0.0
    for term in terms:
        if term in title:
            boost += 0.05
        if term in path:
            boost += 0.03
        if term in content:
            boost += 0.02
    return min(boost, 0.15)


def chunk_to_search_result(doc: dict[str, Any], chunk: dict[str, Any], score: float) -> SearchResult:
    metadata = chunk.get("metadata") or {}
    return SearchResult(
        content=chunk.get("content", ""),
        title=metadata.get("title") or doc.get("title", ""),
        path=metadata.get("path") or doc.get("path", ""),
        source_path=metadata.get("source_path", ""),
        doc_id=metadata.get("doc_id") or doc.get("doc_id", ""),
        chunk_index=metadata.get("chunk_index", 0),
        total_chunks=metadata.get("total_chunks", doc.get("chunk_count", 0)),
        score=score,
    )


def candidate_to_debug_dict(candidate: RetrievalCandidate) -> dict[str, Any]:
    result = candidate.result
    return {
        "content": result.content,
        "title": result.title,
        "path": result.path,
        "source_path": result.source_path,
        "doc_id": result.doc_id,
        "chunk_index": result.chunk_index,
        "total_chunks": result.total_chunks,
        "score": round(candidate.final_score, 4),
        "channel": candidate.channel,
        "raw_score": round(candidate.raw_score, 4),
        "final_score": round(candidate.final_score, 4),
        "postprocess_reason": candidate.postprocess_reason,
    }

"""In-memory keyword inverted index for exact-term retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .retrieval import (
    RetrievalCandidate,
    RetrievalQuery,
    chunk_to_search_result,
    doc_matches_filters,
    keyword_score,
    tokenize_keyword_terms,
    normalize_entities,
)


@dataclass
class IndexedChunk:
    doc: dict[str, Any]
    chunk: dict[str, Any]
    terms: set[str]


class KeywordInvertedIndex:
    def __init__(self):
        self._chunks: dict[tuple[str, int], IndexedChunk] = {}
        self._postings: dict[str, set[tuple[str, int]]] = {}
        self._ready = False

    @property
    def ready(self) -> bool:
        """Whether the index represents a complete knowledge-base snapshot."""
        return self._ready

    async def rebuild(self, kb: Any) -> None:
        chunks: dict[tuple[str, int], IndexedChunk] = {}
        postings: dict[str, set[tuple[str, int]]] = {}
        for doc in await kb._doc_index_all():
            doc_id = doc.get("doc_id", "")
            if not doc_id:
                continue
            for chunk in await kb.get_document_chunks(doc_id):
                metadata = chunk.get("metadata") or {}
                chunk_index = int(metadata.get("chunk_index", 0))
                key = (doc_id, chunk_index)
                indexed = IndexedChunk(doc=doc, chunk=chunk, terms=self._terms_for(doc, chunk))
                chunks[key] = indexed
                for term in indexed.terms:
                    postings.setdefault(term, set()).add(key)

        self._chunks = chunks
        self._postings = postings
        self._ready = True

    async def upsert_document(self, kb: Any, doc_id: str) -> None:
        doc = await kb._doc_index_get(doc_id)
        if not doc:
            self.remove_document(doc_id)
            return

        pending: list[tuple[tuple[str, int], IndexedChunk]] = []
        for chunk in await kb.get_document_chunks(doc_id):
            metadata = chunk.get("metadata") or {}
            chunk_index = int(metadata.get("chunk_index", 0))
            key = (doc_id, chunk_index)
            pending.append((
                key,
                IndexedChunk(doc=doc, chunk=chunk, terms=self._terms_for(doc, chunk)),
            ))

        self.remove_document(doc_id)
        for key, indexed in pending:
            self._chunks[key] = indexed
            for term in indexed.terms:
                self._postings.setdefault(term, set()).add(key)

    def remove_document(self, doc_id: str) -> None:
        keys = [key for key in self._chunks if key[0] == doc_id]
        for key in keys:
            indexed = self._chunks.pop(key)
            for term in indexed.terms:
                postings = self._postings.get(term)
                if postings is None:
                    continue
                postings.discard(key)
                if not postings:
                    self._postings.pop(term, None)

    def search(self, query: RetrievalQuery, top_k: int) -> list[RetrievalCandidate]:
        terms = tokenize_keyword_terms(query.text)
        keys: set[tuple[str, int]] = set()
        for term in terms:
            keys.update(self._postings.get(term, set()))

        candidates: list[RetrievalCandidate] = []
        for key in keys:
            indexed = self._chunks.get(key)
            if not indexed or not doc_matches_filters(indexed.doc, query):
                continue
            score = keyword_score(terms, indexed.doc, indexed.chunk.get("content", ""))
            if score <= 0:
                continue
            candidates.append(
                RetrievalCandidate(
                    result=chunk_to_search_result(indexed.doc, indexed.chunk, score),
                    channel="keyword",
                    raw_score=score,
                )
            )
        return sorted(candidates, key=lambda item: item.raw_score, reverse=True)[:top_k]

    def _terms_for(self, doc: dict[str, Any], chunk: dict[str, Any]) -> set[str]:
        return set(tokenize_keyword_terms(" ".join([
            str(doc.get("doc_id", "")),
            str(doc.get("title", "")),
            str(doc.get("path", "")),
            " ".join(normalize_entities(doc.get("entities", []))),
            chunk.get("content", ""),
        ])))

"""Composable retrieval pipeline for multi-channel RAG search."""

from __future__ import annotations

import asyncio
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
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
    association_reason: str = ""


class RetrievalChannel(Protocol):
    name: str
    enabled: bool
    timeout_ms: int

    async def search(self, query: RetrievalQuery) -> list[RetrievalCandidate]:
        ...


class VectorChannel:
    name = "vector"
    enabled = True

    def __init__(self, kb: Any, embedder: Any, timeout_ms: int = 5000):
        self.kb = kb
        self.embedder = embedder
        self.timeout_ms = max(1, int(timeout_ms))

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

    def __init__(
        self,
        kb: Any,
        keyword_index: Any | None = None,
        timeout_ms: int = 3000,
    ):
        self.kb = kb
        self.keyword_index = keyword_index
        self.timeout_ms = max(1, int(timeout_ms))

    async def search(self, query: RetrievalQuery) -> list[RetrievalCandidate]:
        terms = tokenize_keyword_terms(query.text)
        if not terms:
            return []

        if self.keyword_index is not None:
            indexed = self.keyword_index.search(query, query.top_k)
            if self.keyword_index.ready:
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

    def __init__(self, kb: Any, timeout_ms: int = 2000):
        self.kb = kb
        self.timeout_ms = max(1, int(timeout_ms))

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


class GraphAssociationExpander:
    """Expand strong initial hits to related documents from the persisted graph.

    The graph is optional and loaded lazily. A missing or stale graph never blocks the
    vector/keyword/structure channels; document filters are rechecked against the live
    document index before a graph candidate is returned.
    """

    name = "graph"

    def __init__(
        self,
        kb: Any,
        graph_path: str | Path,
        *,
        enabled: bool = True,
        timeout_ms: int = 800,
        weight: float = 0.35,
        max_results: int = 3,
        max_hops: int = 2,
        seed_count: int = 3,
        min_edge_weight: float = 0.25,
    ):
        self.kb = kb
        self.graph_path = Path(graph_path)
        self.enabled = bool(enabled)
        self.timeout_ms = max(1, int(timeout_ms))
        self.weight = min(max(float(weight), 0.0), 1.0)
        self.max_results = max(0, int(max_results))
        self.max_hops = min(max(1, int(max_hops)), 3)
        self.seed_count = max(1, int(seed_count))
        self.min_edge_weight = min(max(float(min_edge_weight), 0.0), 1.0)
        self._cached_mtime_ns = -1
        self._cached_adjacency: dict[str, list[tuple[str, float, str]]] = {}

    @property
    def version(self) -> int:
        path = self._resolved_graph_path()
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    async def expand(
        self,
        candidates: list[RetrievalCandidate],
        query: RetrievalQuery,
    ) -> list[RetrievalCandidate]:
        if not self.enabled or self.max_results == 0 or not candidates:
            return []

        adjacency = await asyncio.to_thread(self._load_adjacency)
        if not adjacency:
            return []

        seeds: list[RetrievalCandidate] = []
        existing_docs = {
            candidate.result.doc_id for candidate in candidates if candidate.result.doc_id
        }
        seen_seed_docs: set[str] = set()
        for candidate in sorted(
            candidates,
            key=lambda item: item.final_score or normalized_score(item),
            reverse=True,
        ):
            doc_id = candidate.result.doc_id
            if not doc_id or doc_id in seen_seed_docs:
                continue
            seen_seed_docs.add(doc_id)
            seeds.append(candidate)
            if len(seeds) >= self.seed_count:
                break

        related: dict[str, tuple[float, str]] = {}
        for seed in seeds:
            seed_id = seed.result.doc_id
            seed_score = seed.final_score or normalized_score(seed)
            frontier = [(seed_id, 1.0, [])]
            best_seen = {seed_id: 1.0}
            for hop in range(1, self.max_hops + 1):
                next_frontier: list[tuple[str, float, list[str]]] = []
                for current_id, path_weight, relations in frontier:
                    for target_id, edge_weight, relation in adjacency.get(current_id, []):
                        if target_id in seen_seed_docs:
                            continue
                        combined = path_weight * edge_weight
                        if hop > 1:
                            combined *= 0.75
                        if combined <= best_seen.get(target_id, 0.0):
                            continue
                        best_seen[target_id] = combined
                        path_relations = [*relations, relation]
                        next_frontier.append((target_id, combined, path_relations))
                        score = min(seed_score * combined * self.weight, 1.0)
                        reason = (
                            f"seed={seed_id}; hops={hop}; relations="
                            f"{' -> '.join(path_relations)}; edge_score={combined:.4f}"
                        )
                        if (
                            target_id not in existing_docs
                            and score > related.get(target_id, (0.0, ""))[0]
                        ):
                            related[target_id] = (score, reason)
                frontier_limit = max(50, self.max_results * 20)
                frontier = sorted(
                    next_frontier, key=lambda item: item[1], reverse=True
                )[:frontier_limit]
                if not frontier:
                    break

        expanded: list[RetrievalCandidate] = []
        query_terms = tokenize_keyword_terms(query.text)
        for doc_id, (score, reason) in sorted(
            related.items(), key=lambda item: item[1][0], reverse=True
        ):
            doc = await self.kb._doc_index_get(doc_id)
            if not doc or not doc_matches_filters(doc, query):
                continue
            chunks = await self.kb.get_document_chunks(doc_id)
            if not chunks:
                continue
            chunk = max(
                chunks,
                key=lambda item: keyword_score(
                    query_terms,
                    doc,
                    item.get("content", ""),
                ),
            )
            expanded.append(
                RetrievalCandidate(
                    result=chunk_to_search_result(doc, chunk, score),
                    channel=self.name,
                    raw_score=score,
                    association_reason=reason,
                )
            )
            if len(expanded) >= self.max_results:
                break
        return expanded

    def _resolved_graph_path(self) -> Path:
        if self.graph_path.exists():
            return self.graph_path
        fallback = self.graph_path.with_name("graph.json")
        return fallback if fallback.exists() else self.graph_path

    def _load_adjacency(self) -> dict[str, list[tuple[str, float, str]]]:
        path = self._resolved_graph_path()
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            self._cached_mtime_ns = -1
            self._cached_adjacency = {}
            return {}
        if mtime_ns == self._cached_mtime_ns:
            return self._cached_adjacency

        raw = json.loads(path.read_text(encoding="utf-8"))
        edges = raw.get("edges", raw.get("links", []))
        adjacency: dict[str, list[tuple[str, float, str]]] = {}
        default_weights = {
            "semantically_similar": 0.75,
            "co_tag": 0.50,
            "same_directory": 0.35,
        }
        for edge in edges:
            source = edge.get("source", "")
            target = edge.get("target", "")
            if isinstance(source, dict):
                source = source.get("id", "")
            if isinstance(target, dict):
                target = target.get("id", "")
            source, target = str(source), str(target)
            if not source or not target or source == target:
                continue
            relation = str(edge.get("relation") or edge.get("label") or "related")
            try:
                edge_weight = float(edge.get("weight", default_weights.get(relation, 0.30)))
            except (TypeError, ValueError):
                edge_weight = default_weights.get(relation, 0.30)
            edge_weight = min(max(edge_weight, 0.0), 1.0)
            if edge_weight < self.min_edge_weight:
                continue
            adjacency.setdefault(source, []).append((target, edge_weight, relation))
            adjacency.setdefault(target, []).append((source, edge_weight, relation))

        self._cached_mtime_ns = mtime_ns
        self._cached_adjacency = adjacency
        return adjacency


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
            if candidate.association_reason:
                candidate.postprocess_reason += f"; {candidate.association_reason}"
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
        neighbor_timeout_ms: int = 1000,
        graph_expander: GraphAssociationExpander | None = None,
    ):
        self.channels = channels
        self.postprocessor = postprocessor or SearchResultPostProcessor()
        self.kb = kb
        self.neighbor_window = max(0, neighbor_window)
        self.neighbor_timeout_ms = max(1, neighbor_timeout_ms)
        self.graph_expander = graph_expander
        self._last_errors: ContextVar[tuple[dict[str, str], ...]] = ContextVar(
            f"retrieval_errors_{id(self)}",
            default=(),
        )

    @property
    def last_errors(self) -> list[dict[str, str]]:
        return list(self._last_errors.get())

    @property
    def graph_version(self) -> int:
        return self.graph_expander.version if self.graph_expander else 0

    async def search(self, query: RetrievalQuery) -> list[dict[str, Any]]:
        query = rewrite_query(query)
        candidates: list[RetrievalCandidate] = []
        errors: list[dict[str, str]] = []
        self._last_errors.set(())
        enabled_channels = [channel for channel in self.channels if channel.enabled]
        outcomes = await asyncio.gather(
            *(
                asyncio.wait_for(
                    channel.search(query),
                    timeout=max(1, channel.timeout_ms) / 1000,
                )
                for channel in enabled_channels
            ),
            return_exceptions=True,
        )
        for channel, outcome in zip(enabled_channels, outcomes):
            if isinstance(outcome, TimeoutError):
                errors.append({
                    "channel": channel.name,
                    "error": f"timed out after {channel.timeout_ms}ms",
                })
            elif isinstance(outcome, Exception):
                errors.append({
                    "channel": channel.name,
                    "error": str(outcome),
                })
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                candidates.extend(outcome)
        if self.kb and self.neighbor_window > 0:
            try:
                candidates.extend(await asyncio.wait_for(
                    self._expand_neighbors(candidates),
                    timeout=self.neighbor_timeout_ms / 1000,
                ))
            except TimeoutError:
                errors.append({
                    "channel": "neighbor_expansion",
                    "error": f"timed out after {self.neighbor_timeout_ms}ms",
                })
            except Exception as exc:
                errors.append({
                    "channel": "neighbor_expansion",
                    "error": str(exc),
                })

        if self.graph_expander and self.graph_expander.enabled:
            try:
                seed_candidates = self.postprocessor.process(list(candidates), query)
                candidates.extend(await asyncio.wait_for(
                    self.graph_expander.expand(seed_candidates, query),
                    timeout=self.graph_expander.timeout_ms / 1000,
                ))
            except TimeoutError:
                errors.append({
                    "channel": "graph",
                    "error": f"timed out after {self.graph_expander.timeout_ms}ms",
                })
            except Exception as exc:
                errors.append({"channel": "graph", "error": str(exc)})
        self._last_errors.set(tuple(errors))

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


def tokenize_keyword_terms(query: str) -> list[str]:
    """Tokenize exact terms and add CJK bigrams for partial phrase recall."""
    terms: list[str] = []
    seen: set[str] = set()
    for token in tokenize_query(query):
        if token not in seen:
            seen.add(token)
            terms.append(token)
        for cjk_run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", token):
            for index in range(len(cjk_run) - 1):
                bigram = cjk_run[index:index + 2]
                if bigram in seen:
                    continue
                seen.add(bigram)
                terms.append(bigram)
    return terms


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
    if candidate.channel == "graph":
        return min(max(candidate.raw_score, 0.0), 1.0)
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
    item = {
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
    if candidate.association_reason:
        item["association_reason"] = candidate.association_reason
    return item

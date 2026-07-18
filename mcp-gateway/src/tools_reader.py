"""Read-only KnowledgeTools: search, list, and get document operations."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from fastapi import HTTPException

from config import get_settings
from document_metadata import normalize_metadata_values
from knowledge_base import KnowledgeBase
from source_store import SourceStore
from embedding import OllamaEmbedder
from directory_tree import DirectoryTree
from logger import get_logger
from rag.retrieval import (
    GraphAssociationExpander,
    KeywordChannel,
    RetrievalPipeline,
    RetrievalQuery,
    StructureChannel,
    VectorChannel,
)
from rag.keyword_index import KeywordInvertedIndex

logger = get_logger()

SEARCH_CACHE_VERSION_KEY = "kb:search_cache_version"
MAX_CONTEXT_CHARS = 20_000


class KnowledgeToolsReader:
    """Read-only knowledge base operations safe for concurrent access."""

    def __init__(
        self,
        kb: KnowledgeBase,
        embedder: OllamaEmbedder,
        source_store: SourceStore,
        redis_client=None,
    ):
        self.kb = kb
        self.embedder = embedder
        self.source_store = source_store
        self.redis = redis_client
        self.settings = get_settings().model_copy()
        self.keyword_index = KeywordInvertedIndex()
        self._keyword_index_refresh_lock = asyncio.Lock()
        self._search_capacity = asyncio.Semaphore(max(1, self.settings.SEARCH_MAX_CONCURRENCY))
        self.retrieval_pipeline = RetrievalPipeline(
            channels=[
                VectorChannel(
                    kb,
                    embedder,
                    timeout_ms=self.settings.SEARCH_VECTOR_TIMEOUT_MS,
                ),
                KeywordChannel(
                    kb,
                    self.keyword_index,
                    timeout_ms=self.settings.SEARCH_KEYWORD_TIMEOUT_MS,
                ),
                StructureChannel(
                    kb,
                    timeout_ms=self.settings.SEARCH_STRUCTURE_TIMEOUT_MS,
                ),
            ],
            kb=kb,
            neighbor_window=1,
            neighbor_timeout_ms=self.settings.SEARCH_NEIGHBOR_TIMEOUT_MS,
            graph_expander=GraphAssociationExpander(
                kb,
                Path(self.settings.KBDATA_DIR or "kbdata") / "graph" / "retrieval_index.json",
                enabled=self.settings.GRAPH_RETRIEVAL_ENABLED,
                timeout_ms=self.settings.GRAPH_RETRIEVAL_TIMEOUT_MS,
                weight=self.settings.GRAPH_RETRIEVAL_WEIGHT,
                max_results=self.settings.GRAPH_RETRIEVAL_MAX_RESULTS,
                max_hops=self.settings.GRAPH_RETRIEVAL_MAX_HOPS,
                seed_count=self.settings.GRAPH_RETRIEVAL_SEED_COUNT,
                min_edge_weight=self.settings.GRAPH_RETRIEVAL_MIN_EDGE_WEIGHT,
            ),
        )

    async def refresh_keyword_index(self) -> None:
        """Rebuild the in-memory keyword index outside the read path."""
        async with self._keyword_index_refresh_lock:
            await self.keyword_index.rebuild(self.kb)

    async def refresh_keyword_index_safely(self, reason: str = "") -> None:
        try:
            await self.refresh_keyword_index()
        except Exception as e:
            suffix = f" after {reason}" if reason else ""
            logger.warning(f"Failed to refresh keyword index{suffix}: {e}")
        finally:
            await self.invalidate_search_cache_safely(reason)

    async def refresh_keyword_document(self, doc_id: str) -> None:
        async with self._keyword_index_refresh_lock:
            await self.keyword_index.upsert_document(self.kb, doc_id)

    async def refresh_keyword_document_safely(
        self,
        doc_id: str,
        reason: str = "",
    ) -> None:
        try:
            await self.refresh_keyword_document(doc_id)
        except Exception as e:
            suffix = f" after {reason}" if reason else ""
            logger.warning(f"Failed to refresh keyword document{suffix}: {e}")
        finally:
            await self.invalidate_search_cache_safely(reason)

    async def remove_keyword_document(self, doc_id: str) -> None:
        async with self._keyword_index_refresh_lock:
            self.keyword_index.remove_document(doc_id)

    async def remove_keyword_document_safely(
        self,
        doc_id: str,
        reason: str = "",
    ) -> None:
        try:
            await self.remove_keyword_document(doc_id)
        except Exception as e:
            suffix = f" after {reason}" if reason else ""
            logger.warning(f"Failed to remove keyword document{suffix}: {e}")
        finally:
            await self.invalidate_search_cache_safely(reason)

    async def invalidate_search_cache(self) -> None:
        if not self.redis:
            return
        try:
            await self.redis.incr(SEARCH_CACHE_VERSION_KEY)
        except Exception as e:
            logger.warning(f"Failed to invalidate search cache: {e}")

    async def invalidate_search_cache_safely(self, reason: str = "") -> None:
        try:
            await self.invalidate_search_cache()
        except Exception as e:
            suffix = f" after {reason}" if reason else ""
            logger.warning(f"Failed to invalidate search cache{suffix}: {e}")

    async def _search_cache_version(self) -> str:
        if not self.redis:
            return "0"
        try:
            return str(await self.redis.get(SEARCH_CACHE_VERSION_KEY) or "0")
        except Exception as e:
            logger.warning(f"Failed to read search cache version: {e}")
            return "0"

    async def _search_cache_key(
        self,
        query: str,
        top_k: int,
        filter_tags: list[str],
        filter_path: str,
        include_context: bool,
        max_context_chars: int,
    ) -> str:
        payload = {
            "version": await self._search_cache_version(),
            "query": " ".join(query.strip().split()),
            "top_k": top_k,
            "filter_tags": sorted(filter_tags),
            "filter_path": filter_path,
            "include_context": include_context,
            "max_context_chars": max_context_chars,
            "graph_version": self.retrieval_pipeline.graph_version,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return f"kb:search_cache:{digest}"

    async def _record_search_stats(self) -> None:
        if not self.redis:
            return
        try:
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")
            key = f"stats:search:{today}"
            count = await self.redis.incr(key)
            if count == 1:
                await self.redis.expire(key, 86400 * 90)

            hourly_key = f"stats:search:hourly:{now.strftime('%Y-%m-%d:%H')}"
            hourly_count = await self.redis.incr(hourly_key)
            if hourly_count == 1:
                await self.redis.expire(hourly_key, 86400 * 8)
        except Exception as e:
            logger.warning(f"Failed to record search stats in Redis: {e}")

    @staticmethod
    async def _notify_progress(progress_callback, progress: float, message: str) -> None:
        if progress_callback is None:
            return
        try:
            await progress_callback(progress, message)
        except Exception:
            # Progress is advisory and must never fail the search result.
            return

    async def search_knowledge(
        self,
        query: str,
        top_k: int = 5,
        filter_tags: list[str] | None = None,
        filter_path: str = "",
        include_context: bool = True,
        max_context_chars: int | None = None,
        progress_callback=None,
    ) -> dict:
        """Search the knowledge base."""
        if not query or not query.strip():
            raise HTTPException(status_code=400, detail="Query cannot be empty")
        if max_context_chars is None:
            max_context_chars = max(0, min(
                self.settings.SEARCH_CONTEXT_MAX_CHARS,
                MAX_CONTEXT_CHARS,
            ))
        elif max_context_chars < 0 or max_context_chars > MAX_CONTEXT_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"max_context_chars must be between 0 and {MAX_CONTEXT_CHARS}",
            )
        if not include_context:
            max_context_chars = 0

        filter_tags = filter_tags or []
        started = monotonic()
        timeout_ms = max(1, self.settings.SEARCH_TOTAL_TIMEOUT_MS)
        queue_timeout_ms = max(1, self.settings.SEARCH_QUEUE_TIMEOUT_MS)
        await self._notify_progress(progress_callback, 5, "等待检索执行槽位")
        try:
            await asyncio.wait_for(
                self._search_capacity.acquire(),
                timeout=queue_timeout_ms / 1000,
            )
        except TimeoutError:
            await self._notify_progress(progress_callback, 100, "检索队列繁忙，请稍后重试")
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "搜索请求繁忙，请稍后重试",
                    "retry_after_ms": queue_timeout_ms,
                },
            )

        await self._notify_progress(progress_callback, 15, "已获得执行槽位，检查查询缓存")
        try:
            try:
                return await asyncio.wait_for(
                    self._search_knowledge(
                        query=query,
                        top_k=top_k,
                        filter_tags=filter_tags,
                        filter_path=filter_path,
                        include_context=include_context,
                        max_context_chars=max_context_chars,
                        progress_callback=progress_callback,
                    ),
                    timeout=timeout_ms / 1000,
                )
            except TimeoutError:
                await self._notify_progress(progress_callback, 90, "检索超时，返回降级结果")
                return {
                    "query": query,
                    "results": [],
                    "total": 0,
                    "retrieval_errors": [{
                        "channel": "request",
                        "error": f"timed out after {timeout_ms}ms",
                    }],
                    "cache_hit": False,
                    "status": "degraded",
                    "timed_out": True,
                    "timings_ms": {"total": round((monotonic() - started) * 1000, 3)},
                }
        finally:
            self._search_capacity.release()

    async def _search_knowledge(
        self,
        query: str,
        top_k: int,
        filter_tags: list[str],
        filter_path: str,
        include_context: bool,
        max_context_chars: int,
        progress_callback=None,
    ) -> dict:
        started = monotonic()
        await self._record_search_stats()

        cache_key = ""
        if self.redis and self.settings.SEARCH_CACHE_TTL > 0:
            cache_key = await self._search_cache_key(
                query,
                top_k,
                filter_tags,
                filter_path,
                include_context,
                max_context_chars,
            )
            try:
                cached = await self.redis.get(cache_key)
                if cached:
                    result = json.loads(cached)
                    result["cache_hit"] = True
                    result.setdefault("status", "ok")
                    result.setdefault("timed_out", False)
                    result["timings_ms"] = {
                        "retrieval": 0.0,
                        "enrichment": 0.0,
                        "total": round((monotonic() - started) * 1000, 3),
                    }
                    await self._notify_progress(progress_callback, 90, "命中查询缓存，准备返回")
                    return result
            except Exception as e:
                logger.warning(f"Failed to read search cache: {e}")

        await self._notify_progress(progress_callback, 35, "执行向量、关键词和结构混合检索")
        retrieval_started = monotonic()
        results = await self.retrieval_pipeline.search(
            RetrievalQuery(
                text=query,
                top_k=top_k,
                filter_tags=filter_tags,
                filter_path=filter_path,
            )
        )
        retrieval_ms = round((monotonic() - retrieval_started) * 1000, 3)

        await self._notify_progress(
            progress_callback,
            75,
            f"基础检索完成，命中 {len(results)} 条，补充上下文",
        )
        enrichment_started = monotonic()
        retrieval_errors = self.retrieval_pipeline.last_errors
        enrichment_timeout_ms = max(1, self.settings.SEARCH_ENRICH_TIMEOUT_MS)
        try:
            enriched_results = await asyncio.wait_for(
                self._enrich_search_results(
                    results,
                    include_context=include_context,
                    max_context_chars=max_context_chars,
                ),
                timeout=enrichment_timeout_ms / 1000,
            )
        except TimeoutError:
            retrieval_errors.append({
                "channel": "enrichment",
                "error": f"timed out after {enrichment_timeout_ms}ms",
            })
            enriched_results = self._results_without_context(results)
        enrichment_ms = round((monotonic() - enrichment_started) * 1000, 3)
        await self._notify_progress(progress_callback, 90, "检索结果整理完成")
        timed_out = any("timed out" in error.get("error", "") for error in retrieval_errors)

        result = {
            "query": query,
            "results": enriched_results,
            "total": len(results),
            "retrieval_errors": retrieval_errors,
            "cache_hit": False,
            "status": "degraded" if retrieval_errors else "ok",
            "timed_out": timed_out,
            "timings_ms": {
                "retrieval": retrieval_ms,
                "enrichment": enrichment_ms,
                "total": round((monotonic() - started) * 1000, 3),
            },
        }

        if cache_key and not result["retrieval_errors"]:
            try:
                await self.redis.set(
                    cache_key,
                    json.dumps(result, ensure_ascii=False),
                    ex=self.settings.SEARCH_CACHE_TTL,
                )
            except Exception as e:
                logger.warning(f"Failed to write search cache: {e}")

        return result

    @staticmethod
    def _results_without_context(items: list[dict]) -> list[dict]:
        fallback = []
        for item in items:
            chunk_index = int(item.get("chunk_index", 0) or 0)
            enriched = dict(item)
            enriched.setdefault("excerpt", item.get("content", ""))
            enriched.setdefault("context_before", "")
            enriched.setdefault("context_after", "")
            enriched.setdefault("context_truncated", False)
            enriched.setdefault("updated_at", "")
            enriched.setdefault("tags", [])
            enriched.setdefault("entities", [])
            enriched.setdefault(
                "citation",
                f"{item.get('path') or '/'}:{item.get('title', '')}#chunk-{chunk_index}",
            )
            fallback.append(enriched)
        return fallback

    async def _enrich_search_results(
        self,
        items: list[dict],
        include_context: bool = True,
        max_context_chars: int = 2000,
    ) -> list[dict]:
        doc_ids = list(dict.fromkeys(
            item.get("doc_id", "") for item in items if item.get("doc_id", "")
        ))

        async def prefetch(doc_id: str) -> tuple[list[dict], dict]:
            try:
                if include_context and max_context_chars > 0:
                    chunks, doc_info = await asyncio.gather(
                        self.kb.get_document_chunks(doc_id),
                        self.kb._doc_index_get(doc_id),
                    )
                else:
                    chunks = []
                    doc_info = await self.kb._doc_index_get(doc_id)
                return chunks, doc_info or {}
            except Exception:
                return [], {}

        prefetched = await asyncio.gather(*(prefetch(doc_id) for doc_id in doc_ids))
        data_by_doc = dict(zip(doc_ids, prefetched))
        enriched_results = []
        for item in items:
            doc_id = item.get("doc_id", "")
            chunk_index = int(item.get("chunk_index", 0) or 0)
            chunks, doc_info = data_by_doc.get(doc_id, ([], {}))
            by_index = {
                int((chunk.get("metadata") or {}).get("chunk_index", 0)): chunk
                for chunk in chunks
            }
            enriched = dict(item)
            enriched.setdefault("excerpt", item.get("content", ""))
            context_before = by_index.get(chunk_index - 1, {}).get("content", "")
            context_after = by_index.get(chunk_index + 1, {}).get("content", "")
            bounded_before, bounded_after = self._bound_neighbor_context(
                context_before,
                context_after,
                max_context_chars,
            )
            enriched["context_before"] = bounded_before
            enriched["context_after"] = bounded_after
            enriched["context_truncated"] = (
                len(context_before) + len(context_after) > max_context_chars
            )
            enriched["updated_at"] = doc_info.get("updated_at", "")
            enriched["tags"] = normalize_metadata_values(doc_info.get("tags", []))
            enriched["entities"] = normalize_metadata_values(doc_info.get("entities", []))
            enriched["citation"] = f"{item.get('path') or '/'}:{item.get('title', '')}#chunk-{chunk_index}"
            enriched_results.append(enriched)
        return enriched_results

    @staticmethod
    def _bound_neighbor_context(
        context_before: str,
        context_after: str,
        max_chars: int,
    ) -> tuple[str, str]:
        if max_chars <= 0:
            return "", ""

        before_chars = min(len(context_before), max_chars // 2)
        after_chars = min(len(context_after), max_chars - before_chars)
        remaining = max_chars - before_chars - after_chars
        if remaining:
            extra_before = min(len(context_before) - before_chars, remaining)
            before_chars += extra_before
            remaining -= extra_before
        if remaining:
            after_chars += min(len(context_after) - after_chars, remaining)

        bounded_before = context_before[-before_chars:] if before_chars else ""
        bounded_after = context_after[:after_chars] if after_chars else ""
        return bounded_before, bounded_after

    async def list_documents(
        self,
        tags: list[str] | None = None,
        path: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """List documents with pagination."""
        results, total = await self.kb.list_documents(
            tags=tags,
            path=path,
            limit=limit,
            offset=offset,
        )
        return {
            "documents": [
                {
                    "doc_id": d.doc_id,
                    "title": d.title,
                    "path": d.path,
                    "tags": d.tags,
                    "entities": d.entities,
                    "chunk_count": d.chunk_count,
                    "created_at": d.created_at,
                    "updated_at": d.updated_at,
                }
                for d in results
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def list_directories(self) -> dict:
        """List directory tree."""
        docs, _ = await self.kb.list_documents(limit=10000, offset=0)
        metadatas = [{"path": d.path} for d in docs]
        tree = DirectoryTree.build_from_metadata(metadatas)
        return {"tree": tree}

    def _read_source_content(self, doc_id: str, path: str, source_path: str = "") -> str:
        """Read source content, tolerating legacy stale ``source_path`` metadata."""
        if source_path:
            try:
                content = self.source_store.get_source_by_full_path(source_path) or ""
                if content:
                    return content
            except Exception as exc:
                logger.warning(
                    "Failed to read source_path for doc_id=%s; trying current document path: %s",
                    doc_id,
                    exc,
                )

        try:
            return self.source_store.get_source(doc_id, path) or ""
        except Exception as exc:
            logger.warning("Failed to read source content for doc_id=%s: %s", doc_id, exc)
            return ""

    async def get_document(self, doc_id: str) -> dict:
        """Get full document info including content, tags, and chunks."""
        if not doc_id:
            raise HTTPException(status_code=400, detail="Document ID cannot be empty")

        doc_info = await self.kb._doc_index_get(doc_id)
        if not doc_info:
            raise HTTPException(
                status_code=404,
                detail=f"Document not found: doc_id={doc_id}",
            )

        path = doc_info.get("path", "")
        source_path = ""
        content = ""

        chunks = await self.kb.get_document_chunks(doc_id)
        if chunks:
            source_path = chunks[0]["metadata"].get("source_path", "")

        if chunks:
            content = self._read_source_content(doc_id, path, source_path)
            if not content:
                content = "\n\n".join(ch.get("content", "") for ch in chunks)

        chunk_list = []
        for ch in chunks:
            meta = ch.get("metadata", {})
            chunk_list.append({
                "chunk_index": meta.get("chunk_index", 0),
                "content": ch.get("content", ""),
                "metadata": {
                    "total_chunks": meta.get("total_chunks", 0),
                    "source_format": meta.get("source_format", ""),
                },
            })

        return {
            "doc_id": doc_id,
            "title": doc_info.get("title", ""),
            "content": content,
            "path": path,
            "tags": normalize_metadata_values(doc_info.get("tags", [])),
            "entities": normalize_metadata_values(doc_info.get("entities", [])),
            "created_by": doc_info.get("created_by", ""),
            "created_at": doc_info.get("created_at", ""),
            "updated_at": doc_info.get("updated_at", ""),
            "chunks": chunk_list,
        }

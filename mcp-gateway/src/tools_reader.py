"""Read-only KnowledgeTools: search, list, and get document operations."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone

from fastapi import HTTPException

from config import get_settings
from knowledge_base import KnowledgeBase
from source_store import SourceStore
from embedding import OllamaEmbedder
from directory_tree import DirectoryTree
from logger import get_logger
from rag.retrieval import (
    KeywordChannel,
    RetrievalPipeline,
    RetrievalQuery,
    StructureChannel,
    VectorChannel,
)
from rag.keyword_index import KeywordInvertedIndex

logger = get_logger()

SEARCH_CACHE_VERSION_KEY = "kb:search_cache_version"


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
        self.settings = get_settings()
        self.keyword_index = KeywordInvertedIndex()
        self._keyword_index_refresh_lock = asyncio.Lock()
        self.retrieval_pipeline = RetrievalPipeline(
            channels=[
                VectorChannel(kb, embedder),
                KeywordChannel(kb, self.keyword_index),
                StructureChannel(kb),
            ],
            kb=kb,
            neighbor_window=1,
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
    ) -> str:
        payload = {
            "version": await self._search_cache_version(),
            "query": " ".join(query.strip().split()),
            "top_k": top_k,
            "filter_tags": sorted(filter_tags),
            "filter_path": filter_path,
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

    async def search_knowledge(
        self,
        query: str,
        top_k: int = 5,
        filter_tags: list[str] | None = None,
        filter_path: str = "",
    ) -> dict:
        """Search the knowledge base."""
        if not query or not query.strip():
            raise HTTPException(status_code=400, detail="Query cannot be empty")

        filter_tags = filter_tags or []
        await self._record_search_stats()

        cache_key = ""
        if self.redis and self.settings.SEARCH_CACHE_TTL > 0:
            cache_key = await self._search_cache_key(query, top_k, filter_tags, filter_path)
            try:
                cached = await self.redis.get(cache_key)
                if cached:
                    result = json.loads(cached)
                    result["cache_hit"] = True
                    return result
            except Exception as e:
                logger.warning(f"Failed to read search cache: {e}")

        results = await self.retrieval_pipeline.search(
            RetrievalQuery(
                text=query,
                top_k=top_k,
                filter_tags=filter_tags,
                filter_path=filter_path,
            )
        )

        result = {
            "query": query,
            "results": [await self._enrich_search_result(item) for item in results],
            "total": len(results),
            "retrieval_errors": self.retrieval_pipeline.last_errors,
            "cache_hit": False,
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

    async def _enrich_search_result(self, item: dict) -> dict:
        doc_id = item.get("doc_id", "")
        chunk_index = int(item.get("chunk_index", 0) or 0)
        chunks = []
        doc_info = {}
        if doc_id:
            try:
                chunks = await self.kb.get_document_chunks(doc_id)
                doc_info = await self.kb._doc_index_get(doc_id) or {}
            except Exception:
                chunks = []
        by_index = {
            int((chunk.get("metadata") or {}).get("chunk_index", 0)): chunk
            for chunk in chunks
        }
        enriched = dict(item)
        enriched.setdefault("excerpt", item.get("content", ""))
        enriched["context_before"] = by_index.get(chunk_index - 1, {}).get("content", "")
        enriched["context_after"] = by_index.get(chunk_index + 1, {}).get("content", "")
        enriched["updated_at"] = doc_info.get("updated_at", "")
        enriched["tags"] = doc_info.get("tags", [])
        enriched["citation"] = f"{item.get('path') or '/'}:{item.get('title', '')}#chunk-{chunk_index}"
        return enriched

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
            try:
                if source_path:
                    content = self.source_store.get_source_by_full_path(source_path) or ""
                else:
                    content = self.source_store.get_source(doc_id, path) or ""
            except Exception as e:
                logger.warning(f"Failed to read source content for doc_id={doc_id}: {e}")
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

        tags_raw = doc_info.get("tags", "")
        if isinstance(tags_raw, str) and tags_raw:
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        elif isinstance(tags_raw, list):
            tags = tags_raw
        else:
            tags = []

        return {
            "doc_id": doc_id,
            "title": doc_info.get("title", ""),
            "content": content,
            "path": path,
            "tags": tags,
            "created_by": doc_info.get("created_by", ""),
            "created_at": doc_info.get("created_at", ""),
            "updated_at": doc_info.get("updated_at", ""),
            "chunks": chunk_list,
        }

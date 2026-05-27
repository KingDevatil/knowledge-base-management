"""Read-only KnowledgeTools — search, list, get document operations (no write lock needed)."""

from fastapi import HTTPException

from knowledge_base import KnowledgeBase
from source_store import SourceStore
from embedding import OllamaEmbedder, EmbeddingError
from directory_tree import DirectoryTree
from logger import get_logger

logger = get_logger()


class KnowledgeToolsReader:
    """Read-only knowledge base operations — safe for concurrent access."""

    def __init__(
        self,
        kb: KnowledgeBase,
        embedder: OllamaEmbedder,
        source_store: SourceStore,
    ):
        self.kb = kb
        self.embedder = embedder
        self.source_store = source_store

    async def search_knowledge(
        self,
        query: str,
        top_k: int = 5,
        filter_tags: list[str] | None = None,
        filter_path: str = "",
    ) -> dict:
        """Vector search the knowledge base."""
        if not query or not query.strip():
            raise HTTPException(status_code=400, detail="查询内容不能为空")

        try:
            query_embedding = await self.embedder.embed_single(query)
        except EmbeddingError as e:
            raise HTTPException(
                status_code=503,
                detail=f"Embedding 服务异常，无法生成查询向量。{e}",
            )
        if not query_embedding:
            raise HTTPException(
                status_code=503,
                detail="Embedding 服务不可用，请检查 Ollama 是否正常运行",
            )

        results = await self.kb.search(
            query_embedding=query_embedding,
            top_k=top_k,
            filter_tags=filter_tags,
            filter_path=filter_path,
        )

        return {
            "query": query,
            "results": [
                {
                    "content": r.content,
                    "title": r.title,
                    "path": r.path,
                    "source_path": r.source_path,
                    "doc_id": r.doc_id,
                    "chunk_index": r.chunk_index,
                    "total_chunks": r.total_chunks,
                    "score": round(r.score, 4),
                }
                for r in results
            ],
            "total": len(results),
        }

    async def list_documents(
        self,
        tags: list[str] | None = None,
        path: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """List documents with pagination."""
        results = await self.kb.list_documents(
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
            "total": len(results),
            "limit": limit,
            "offset": offset,
        }

    async def list_directories(self) -> dict:
        """List directory tree."""
        docs = await self.kb.list_documents(limit=10000, offset=0)
        metadatas = [{"path": d.path} for d in docs]
        tree = DirectoryTree.build_from_metadata(metadatas)
        return {"tree": tree}

    async def get_document(self, doc_id: str) -> dict:
        """Get full document info including content, tags, and chunks."""
        if not doc_id:
            raise HTTPException(status_code=400, detail="文档 ID 不能为空")

        doc_info = await self.kb._doc_index_get(doc_id)
        if not doc_info:
            raise HTTPException(
                status_code=404,
                detail=f"文档 (doc_id={doc_id}) 不存在，可能已被删除",
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
                logger.warning(
                    f"Failed to read source content for doc_id={doc_id}: {e}"
                )
                content = "\n\n".join(
                    ch.get("content", "") for ch in chunks
                )

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

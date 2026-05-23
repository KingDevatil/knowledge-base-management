import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Request, HTTPException

from config import get_settings
from knowledge_base import KnowledgeBase
from source_store import SourceStore
from embedding import OllamaEmbedder
from chunker import chunk_markdown
from lock import WriteLock, WriteLockError
from directory_tree import DirectoryTree
from auth import APIKeyAuth
from logger import get_logger

logger = get_logger()


class KnowledgeTools:
    """MCP 工具实现"""

    def __init__(
        self,
        kb: KnowledgeBase,
        source_store: SourceStore,
        embedder: OllamaEmbedder,
        write_lock: WriteLock,
        api_key_auth: APIKeyAuth,
    ):
        self.kb = kb
        self.source_store = source_store
        self.embedder = embedder
        self.write_lock = write_lock
        self.api_key_auth = api_key_auth
        self.settings = get_settings()

    # ---------- 读操作（无需锁）----------

    async def search_knowledge(
        self,
        query: str,
        top_k: int = 5,
        filter_tags: list[str] | None = None,
        filter_path: str = "",
    ) -> dict:
        """向量检索知识库"""
        if not query or not query.strip():
            raise HTTPException(status_code=400, detail="查询内容不能为空")

        # 生成查询向量
        query_embedding = await self.embedder.embed_single(query)
        if not query_embedding:
            raise HTTPException(status_code=503, detail="Embedding 服务不可用")

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
        """列出文档（分页）"""
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
        """列出目录树"""
        docs = await self.kb.list_documents(limit=10000, offset=0)
        metadatas = [{"path": d.path} for d in docs]
        tree = DirectoryTree.build_from_metadata(metadatas)
        return {"tree": tree}

    # ---------- 写操作（需锁保护）----------

    async def _import_document(
        self,
        title: str,
        content: str,
        path: str,
        tags: list[str],
        created_by: str = "system",
        doc_id: str | None = None,
    ) -> str:
        """内部方法：导入文档（生成embedding、切片、写Chroma）"""
        if not title or not title.strip():
            raise HTTPException(status_code=400, detail="文档标题不能为空")
        if not content or not content.strip():
            raise HTTPException(status_code=400, detail="文档内容不能为空")

        doc_id = doc_id or str(uuid.uuid4())
        path = DirectoryTree.validate_path(path)
        now = datetime.now(timezone.utc).isoformat()

        # 1. 保存源文件到 MinIO
        source_path = self.source_store.save_source(doc_id, content, path)

        # 2. 切片（在锁外完成耗时操作）
        chunks = chunk_markdown(content, self.settings.CHUNK_SIZE, self.settings.CHUNK_OVERLAP)
        if not chunks:
            raise HTTPException(status_code=400, detail="文档内容无法切片，请检查内容")

        # 3. 生成 Embedding（在锁外完成）
        embeddings = await self.embedder.embed(chunks)
        if not embeddings or len(embeddings) != len(chunks):
            raise HTTPException(status_code=503, detail="Embedding 生成失败")

        # 4. 获取写锁并写入 Chroma
        try:
            async with self.write_lock:
                metadata = {
                    "path": path,
                    "tags": tags,
                    "source_path": source_path,
                    "source_format": "markdown",
                    "created_at": now,
                    "updated_at": now,
                    "created_by": created_by,
                    "updated_by": created_by,
                }
                await self.kb.add_document_chunks(
                    doc_id=doc_id,
                    title=title,
                    chunks=chunks,
                    embeddings=embeddings,
                    metadata=metadata,
                )
                logger.info(f"Document imported: doc_id={doc_id}, title={title}, path={path}, chunks={len(chunks)}, created_by={created_by}")
        except WriteLockError:
            logger.warning(f"Write lock busy when importing document: title={title}")
            raise HTTPException(status_code=423, detail="写入锁被占用，请稍后重试")

        return doc_id

    async def add_document(
        self,
        title: str,
        content: str,
        path: str = "",
        tags: list[str] | None = None,
        created_by: str = "system",
    ) -> dict:
        """添加新文档"""
        tags = tags or []
        doc_id = await self._import_document(title, content, path, tags, created_by)
        return {
            "success": True,
            "doc_id": doc_id,
            "message": "文档添加成功",
        }

    async def import_markdown(
        self,
        title: str,
        markdown_content: str,
        path: str = "",
        tags: list[str] | None = None,
        created_by: str = "system",
    ) -> dict:
        """导入 Markdown 内容"""
        tags = tags or []
        doc_id = await self._import_document(title, markdown_content, path, tags, created_by)
        return {
            "success": True,
            "doc_id": doc_id,
            "message": "Markdown 导入成功",
        }

    async def update_document(
        self,
        doc_id: str,
        title: str,
        content: str,
        path: str = "",
        tags: list[str] | None = None,
        updated_by: str = "system",
    ) -> dict:
        """更新已有文档"""
        if not doc_id:
            raise HTTPException(status_code=400, detail="文档 ID 不能为空")
        if not title or not title.strip():
            raise HTTPException(status_code=400, detail="文档标题不能为空")

        tags = tags or []
        new_path = DirectoryTree.validate_path(path)
        now = datetime.now(timezone.utc).isoformat()

        # 查找旧文档信息
        old_chunks = await self.kb.get_document_chunks(doc_id)
        if not old_chunks:
            raise HTTPException(status_code=404, detail="文档不存在")

        old_meta = old_chunks[0]["metadata"] if old_chunks else {}
        old_path = old_meta.get("path", "")

        # 1. 保存新源文件到 MinIO
        if old_path and old_path != new_path:
            # 路径变更：移动源文件
            self.source_store.move_source(doc_id, old_path, new_path)
        source_path = self.source_store.save_source(doc_id, content, new_path)

        # 2. 重新切片和 Embedding（锁外）
        chunks = chunk_markdown(content, self.settings.CHUNK_SIZE, self.settings.CHUNK_OVERLAP)
        if not chunks:
            raise HTTPException(status_code=400, detail="文档内容无法切片")

        embeddings = await self.embedder.embed(chunks)
        if not embeddings or len(embeddings) != len(chunks):
            raise HTTPException(status_code=503, detail="Embedding 生成失败")

        # 3. 获取写锁，删除旧切片，写入新切片
        try:
            async with self.write_lock:
                await self.kb.delete_document(doc_id)
                metadata = {
                    "path": new_path,
                    "tags": tags,
                    "source_path": source_path,
                    "source_format": "markdown",
                    "created_at": old_meta.get("created_at", now),
                    "updated_at": now,
                    "created_by": old_meta.get("created_by", updated_by),
                    "updated_by": updated_by,
                }
                await self.kb.add_document_chunks(
                    doc_id=doc_id,
                    title=title,
                    chunks=chunks,
                    embeddings=embeddings,
                    metadata=metadata,
                )
                logger.info(f"Document updated: doc_id={doc_id}, title={title}, path={new_path}, chunks={len(chunks)}, updated_by={updated_by}")
        except WriteLockError:
            logger.warning(f"Write lock busy when updating document: doc_id={doc_id}")
            raise HTTPException(status_code=423, detail="写入锁被占用，请稍后重试")

        return {
            "success": True,
            "doc_id": doc_id,
            "message": "文档更新成功",
        }

    async def delete_document(self, doc_id: str, deleted_by: str = "system") -> dict:
        """删除文档"""
        if not doc_id:
            raise HTTPException(status_code=400, detail="文档 ID 不能为空")

        # 查找文档信息
        chunks = await self.kb.get_document_chunks(doc_id)
        if not chunks:
            raise HTTPException(status_code=404, detail="文档不存在")

        meta = chunks[0]["metadata"] if chunks else {}
        path = meta.get("path", "")
        source_path = meta.get("source_path", "")
        title = meta.get("title", "")

        # 获取写锁并删除
        try:
            async with self.write_lock:
                await self.kb.delete_document(doc_id)
                # 删除 MinIO 源文件
                if source_path:
                    self.source_store.delete_source_by_path(source_path)
                else:
                    self.source_store.delete_source(doc_id, path)
                logger.info(f"Document deleted: doc_id={doc_id}, title={title}, path={path}, deleted_by={deleted_by}")
        except WriteLockError:
            logger.warning(f"Write lock busy when deleting document: doc_id={doc_id}")
            raise HTTPException(status_code=423, detail="写入锁被占用，请稍后重试")

        return {
            "success": True,
            "doc_id": doc_id,
            "message": "文档删除成功",
        }

    async def reindex_document(self, doc_id: str) -> dict:
        """重新切片 & 向量化单个文档"""
        chunks = await self.kb.get_document_chunks(doc_id)
        if not chunks:
            raise HTTPException(status_code=404, detail="文档不存在")

        meta = chunks[0]["metadata"]
        title = meta.get("title", "")
        path = meta.get("path", "")
        source_path = meta.get("source_path", "")
        tags = meta.get("tags", "").split(",") if isinstance(meta.get("tags"), str) else (meta.get("tags") or [])

        # 从 MinIO 读源文件
        try:
            if source_path:
                content = self.source_store.get_source_by_full_path(source_path)
            else:
                content = self.source_store.get_source(doc_id, path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"读取源文件失败: {e}")
        if not content or not content.strip():
            raise HTTPException(status_code=400, detail="源文件内容为空")

        # 重新切片
        new_chunks = chunk_markdown(content, self.settings.CHUNK_SIZE, self.settings.CHUNK_OVERLAP)
        if not new_chunks:
            raise HTTPException(status_code=400, detail="重新切片结果为空")

        # 生成 Embedding
        embeddings = await self.embedder.embed(new_chunks)
        if not embeddings or len(embeddings) != len(new_chunks):
            raise HTTPException(status_code=503, detail="Embedding 生成失败")

        # 写锁保护下，删除旧切片 + 写入新切片
        try:
            async with self.write_lock:
                await self.kb.delete_document(doc_id)
                now = datetime.now(timezone.utc).isoformat()
                metadata = {
                    "path": path,
                    "tags": tags,
                    "source_path": source_path,
                    "source_format": "markdown",
                    "created_at": meta.get("created_at", now),
                    "updated_at": now,
                    "created_by": meta.get("created_by", "system"),
                    "updated_by": "reindex",
                }
                await self.kb.add_document_chunks(
                    doc_id=doc_id,
                    title=title,
                    chunks=new_chunks,
                    embeddings=embeddings,
                    metadata=metadata,
                )
        except WriteLockError:
            raise HTTPException(status_code=423, detail="写入锁被占用，请稍后重试")

        logger.info(f"Document reindexed: doc_id={doc_id}, title={title}, chunks_old={len(chunks)}, chunks_new={len(new_chunks)}")
        return {
            "success": True,
            "doc_id": doc_id,
            "chunks_old": len(chunks),
            "chunks_new": len(new_chunks),
        }

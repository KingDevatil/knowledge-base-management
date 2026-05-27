"""KnowledgeTools — write operations for knowledge base document management."""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from config import get_settings
from knowledge_base import KnowledgeBase
from source_store import SourceStore
from embedding import OllamaEmbedder, EmbeddingError
from chunker import chunk_markdown
from lock import WriteLock, WriteLockError
from directory_tree import DirectoryTree
from directory_store import _load_dirs, _save_dirs
from auth import APIKeyAuth
from helpers import content_hash, content_size_kb
from tools_reader import KnowledgeToolsReader
from logger import get_logger

logger = get_logger()


class KnowledgeTools(KnowledgeToolsReader):
    """MCP 工具实现 — 读操作继承自 KnowledgeToolsReader，写操作在此实现。"""

    def __init__(
        self,
        kb: KnowledgeBase,
        source_store: SourceStore,
        embedder: OllamaEmbedder,
        write_lock: WriteLock,
        api_key_auth: APIKeyAuth,
        redis_client=None,
    ):
        super().__init__(kb, embedder, source_store, redis_client)
        self.write_lock = write_lock
        self.api_key_auth = api_key_auth
        self.settings = get_settings()

    # ---------- 目录管理（需写锁）----------

    async def rename_directory(self, old_path: str, new_path: str) -> dict:
        """重命名目录：移动所有子文档（需写锁保护），同步更新空目录记录"""
        old_path = DirectoryTree.validate_path(old_path)
        new_path = DirectoryTree.validate_path(new_path)
        if not old_path:
            raise HTTPException(status_code=400, detail="不能重命名根目录")
        if old_path == new_path:
            return {"success": True, "moved": 0}

        async with self.write_lock:
            all_docs = await self.kb._doc_index_all()
            matching_docs = [
                d for d in all_docs
                if d.get("path", "") == old_path or d.get("path", "").startswith(old_path + "/")
            ]

            moved = 0
            for doc in matching_docs:
                doc_id = doc.get("doc_id", "")
                doc_path = doc.get("path", "")
                new_doc_path = new_path if doc_path == old_path else new_path + doc_path[len(old_path):]

                self.source_store.move_source(doc_id, doc_path, new_doc_path)
                doc["path"] = new_doc_path
                await self.kb._doc_index_set(doc_id, doc)
                chunks = await self.kb.get_document_chunks(doc_id)
                for ch in chunks:
                    ch["metadata"]["path"] = new_doc_path
                    self.kb.collection.update(ids=[ch["id"]], metadatas=[ch["metadata"]])
                moved += 1

            dirs = _load_dirs()
            new_dirs = []
            for d in dirs:
                if d == old_path:
                    new_dirs.append(new_path)
                elif d.startswith(old_path + "/"):
                    new_dirs.append(new_path + d[len(old_path):])
                else:
                    new_dirs.append(d)
            _save_dirs(new_dirs)

        return {"success": True, "moved": moved, "old_path": old_path, "new_path": new_path}

    async def delete_directory(self, path: str) -> dict:
        """删除目录：所有文档移至根目录（需写锁保护），同时清除空目录记录"""
        path = DirectoryTree.validate_path(path)
        if not path:
            raise HTTPException(status_code=400, detail="不能删除根目录")

        async with self.write_lock:
            all_docs = await self.kb._doc_index_all()
            matching_docs = [
                d for d in all_docs
                if d.get("path", "") == path or d.get("path", "").startswith(path + "/")
            ]

            moved = 0
            for doc in matching_docs:
                doc_id = doc.get("doc_id", "")
                doc_path = doc.get("path", "")
                self.source_store.move_source(doc_id, doc_path, "")
                doc["path"] = ""
                await self.kb._doc_index_set(doc_id, doc)
                chunks = await self.kb.get_document_chunks(doc_id)
                for ch in chunks:
                    ch["metadata"]["path"] = ""
                    self.kb.collection.update(ids=[ch["id"]], metadatas=[ch["metadata"]])
                moved += 1

            dirs = _load_dirs()
            dirs = [d for d in dirs if d != path and not d.startswith(path + "/")]
            _save_dirs(dirs)

        return {"success": True, "moved_to_root": moved, "deleted_path": path}

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
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        if not title or not title.strip():
            raise HTTPException(status_code=400, detail="文档标题不能为空，请提供有效的标题")
        if not content or not content.strip():
            raise HTTPException(
                status_code=400,
                detail=f"文档「{title}」内容不能为空，请提供有效的 Markdown 内容",
            )

        doc_id = doc_id or str(uuid.uuid4())
        path = DirectoryTree.validate_path(path)
        now = datetime.now(timezone.utc).isoformat()
        size_label = content_size_kb(content)

        source_path = self.source_store.save_source(doc_id, content, path)

        chunks = chunk_markdown(content, self.settings.CHUNK_SIZE, self.settings.CHUNK_OVERLAP)
        if not chunks:
            raise HTTPException(
                status_code=400,
                detail=f"文档「{title}」(doc_id={doc_id}) 内容无法切片。"
                f"内容大小: {size_label}，可能全部为空白字符，请检查内容",
            )

        try:
            embeddings = await self.embedder.embed(chunks)
        except EmbeddingError as e:
            raise HTTPException(
                status_code=503,
                detail=f"文档「{title}」(doc_id={doc_id}) Embedding 生成失败。"
                f"切片数: {len(chunks)}，内容大小: {size_label}。\n{e}",
            )
        if not embeddings or len(embeddings) != len(chunks):
            raise HTTPException(
                status_code=503,
                detail=f"文档「{title}」(doc_id={doc_id}) Embedding 生成失败。"
                f"期望 {len(chunks)} 个向量，实际收到 {len(embeddings) if embeddings else 0} 个。",
            )

        c_hash = content_hash(content)

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
                    doc_id=doc_id, title=title,
                    chunks=chunks, embeddings=embeddings,
                    metadata=metadata,
                )
                await self.kb.set_doc_content_hash(doc_id, c_hash)
                logger.info(
                    f"Document imported: doc_id={doc_id}, title={title}, "
                    f"path={path}, chunks={len(chunks)}, size={size_label}"
                )
        except WriteLockError:
            logger.warning(f"Write lock busy when importing: title={title}, size={size_label}")
            raise HTTPException(
                status_code=423,
                detail=f"知识库写入锁被占用，文档「{title}」暂时无法导入，请稍后重试",
            )

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
        return {"success": True, "doc_id": doc_id, "message": "文档添加成功"}

    async def import_markdown(
        self,
        title: str,
        markdown_content: str,
        path: str = "",
        tags: list[str] | None = None,
        created_by: str = "system",
    ) -> dict:
        """导入 Markdown 内容（委托给 add_document）"""
        return await self.add_document(title, markdown_content, path, tags, created_by)

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
            raise HTTPException(
                status_code=400,
                detail=f"文档 (doc_id={doc_id}) 标题不能为空",
            )

        tags = tags or []
        new_path = DirectoryTree.validate_path(path)
        now = datetime.now(timezone.utc).isoformat()
        size_label = content_size_kb(content)

        old_chunks = await self.kb.get_document_chunks(doc_id)
        if not old_chunks:
            raise HTTPException(
                status_code=404,
                detail=f"文档 (doc_id={doc_id}) 不存在，可能已被删除",
            )

        old_meta = old_chunks[0]["metadata"] if old_chunks else {}
        old_path = old_meta.get("path", "")
        old_title = old_meta.get("title", "")
        old_tags_raw = old_meta.get("tags", "")
        old_tags = (
            [t.strip() for t in old_tags_raw.split(",") if t.strip()]
            if isinstance(old_tags_raw, str) and old_tags_raw
            else (old_tags_raw if isinstance(old_tags_raw, list) else [])
        )

        # 变更检测
        changeless = False
        if new_path == old_path and title == old_title and sorted(tags) == sorted(old_tags):
            new_c_hash = content_hash(content)
            old_c_hash = await self.kb.get_doc_content_hash(doc_id)
            if old_c_hash and new_c_hash == old_c_hash:
                changeless = True
            elif not old_c_hash:
                try:
                    old_source_path = old_meta.get("source_path", "")
                    if old_source_path:
                        old_content = self.source_store.get_source_by_full_path(old_source_path) or ""
                    else:
                        old_content = self.source_store.get_source(doc_id, old_path) or ""
                    if old_content and new_c_hash == content_hash(old_content):
                        changeless = True
                except Exception:
                    pass

        if changeless:
            logger.info(f"Document unchanged, skip: doc_id={doc_id}, title={title}")
            return {
                "success": True, "doc_id": doc_id,
                "message": "内容无变化，已跳过更新",
                "skipped": True, "chunks": len(old_chunks),
            }

        # 保存新源文件
        if old_path and old_path != new_path:
            self.source_store.move_source(doc_id, old_path, new_path)
        source_path = self.source_store.save_source(doc_id, content, new_path)

        chunks = chunk_markdown(content, self.settings.CHUNK_SIZE, self.settings.CHUNK_OVERLAP)
        if not chunks:
            raise HTTPException(
                status_code=400,
                detail=f"文档「{title}」(doc_id={doc_id}) 更新内容无法切片。"
                f"内容大小: {size_label}",
            )

        try:
            embeddings = await self.embedder.embed(chunks)
        except EmbeddingError as e:
            raise HTTPException(
                status_code=503,
                detail=f"文档「{title}」(doc_id={doc_id}) Embedding 失败。\n{e}",
            )

        new_c_hash = content_hash(content)

        try:
            async with self.write_lock:
                await self.kb.mark_doc_updating(doc_id)
                await self.kb.delete_document(doc_id)
                metadata = {
                    "path": new_path, "tags": tags, "source_path": source_path,
                    "source_format": "markdown",
                    "created_at": old_meta.get("created_at", now),
                    "updated_at": now,
                    "created_by": old_meta.get("created_by", updated_by),
                    "updated_by": updated_by,
                }
                await self.kb.add_document_chunks(
                    doc_id=doc_id, title=title,
                    chunks=chunks, embeddings=embeddings,
                    metadata=metadata,
                )
                await self.kb.set_doc_content_hash(doc_id, new_c_hash)
                logger.info(
                    f"Document updated: doc_id={doc_id}, title={title}, "
                    f"path={new_path}, chunks={len(chunks)} (was {len(old_chunks)})"
                )
        except WriteLockError:
            raise HTTPException(
                status_code=423,
                detail=f"写入锁被占用，文档「{title}」暂时无法更新，请稍后重试",
            )

        return {"success": True, "doc_id": doc_id, "message": "文档更新成功"}

    async def delete_document(self, doc_id: str, deleted_by: str = "system") -> dict:
        """删除文档"""
        if not doc_id:
            raise HTTPException(status_code=400, detail="文档 ID 不能为空")

        chunks = await self.kb.get_document_chunks(doc_id)
        if not chunks:
            raise HTTPException(status_code=404, detail=f"文档 (doc_id={doc_id}) 不存在")

        meta = chunks[0]["metadata"] if chunks else {}
        path = meta.get("path", "")
        source_path = meta.get("source_path", "")
        title = meta.get("title", "")

        try:
            async with self.write_lock:
                await self.kb.delete_document(doc_id)
                if source_path:
                    self.source_store.delete_source_by_path(source_path)
                else:
                    self.source_store.delete_source(doc_id, path)
                logger.info(f"Document deleted: doc_id={doc_id}, title={title}")
        except WriteLockError:
            raise HTTPException(
                status_code=423,
                detail=f"写入锁被占用，文档「{title}」暂时无法删除",
            )

        return {"success": True, "doc_id": doc_id, "message": "文档删除成功"}

    async def reindex_document(self, doc_id: str) -> dict:
        """重新切片 & 向量化单个文档"""
        chunks = await self.kb.get_document_chunks(doc_id)
        if not chunks:
            raise HTTPException(status_code=404, detail=f"文档 (doc_id={doc_id}) 不存在")

        meta = chunks[0]["metadata"]
        title = meta.get("title", "")
        path = meta.get("path", "")
        source_path = meta.get("source_path", "")
        tags = (
            meta.get("tags", "").split(",")
            if isinstance(meta.get("tags"), str)
            else (meta.get("tags") or [])
        )

        try:
            if source_path:
                content = self.source_store.get_source_by_full_path(source_path)
            else:
                content = self.source_store.get_source(doc_id, path)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"读取文档「{title}」源文件失败: {e}",
            )
        if not content or not content.strip():
            raise HTTPException(
                status_code=400,
                detail=f"文档「{title}」源文件内容为空",
            )

        size_label = content_size_kb(content)
        new_chunks = chunk_markdown(content, self.settings.CHUNK_SIZE, self.settings.CHUNK_OVERLAP)
        if not new_chunks:
            raise HTTPException(status_code=400, detail=f"文档「{title}」重新切片结果为空")

        try:
            embeddings = await self.embedder.embed(new_chunks)
        except EmbeddingError as e:
            raise HTTPException(status_code=503, detail=f"重建索引 Embedding 失败。\n{e}")

        new_c_hash = content_hash(content)

        try:
            async with self.write_lock:
                await self.kb.mark_doc_updating(doc_id)
                await self.kb.delete_document(doc_id)
                now = datetime.now(timezone.utc).isoformat()
                metadata = {
                    "path": path, "tags": tags, "source_path": source_path,
                    "source_format": "markdown",
                    "created_at": meta.get("created_at", now),
                    "updated_at": now,
                    "created_by": meta.get("created_by", "system"),
                    "updated_by": "reindex",
                }
                await self.kb.add_document_chunks(
                    doc_id=doc_id, title=title,
                    chunks=new_chunks, embeddings=embeddings,
                    metadata=metadata,
                )
                await self.kb.set_doc_content_hash(doc_id, new_c_hash)
        except WriteLockError:
            raise HTTPException(status_code=423, detail=f"写入锁被占用，文档「{title}」暂时无法重建索引")

        logger.info(f"Document reindexed: doc_id={doc_id}, title={title}, old={len(chunks)}, new={len(new_chunks)}")
        return {"success": True, "doc_id": doc_id, "chunks_old": len(chunks), "chunks_new": len(new_chunks)}

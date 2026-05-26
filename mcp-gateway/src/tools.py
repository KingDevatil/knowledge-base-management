import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Request, HTTPException

from config import get_settings
from knowledge_base import KnowledgeBase
from source_store import SourceStore
from embedding import OllamaEmbedder, EmbeddingError
from chunker import chunk_markdown
from lock import WriteLock, WriteLockError
from directory_tree import DirectoryTree
from directory_store import get_user_directories, _load_dirs, _save_dirs
from auth import APIKeyAuth
from logger import get_logger

logger = get_logger()


def _content_hash(content: str) -> str:
    """计算内容 SHA256 哈希（用于变更检测）"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _content_size_kb(content: str) -> str:
    """格式化内容大小（KB）"""
    size = len(content.encode("utf-8"))
    return f"{size / 1024:.1f}KB"


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

    async def get_document(self, doc_id: str) -> dict:
        """获取单个文档的完整信息，包括内容、标签和所有切片"""
        if not doc_id:
            raise HTTPException(status_code=400, detail="文档 ID 不能为空")

        # 1. 从 Redis 索引获取文档元信息
        doc_info = await self.kb._doc_index_get(doc_id)
        if not doc_info:
            raise HTTPException(
                status_code=404,
                detail=f"文档 (doc_id={doc_id}) 不存在，可能已被删除",
            )

        path = doc_info.get("path", "")
        source_path = ""
        content = ""

        # 2. 从 Chroma 获取文档的所有 chunk
        chunks = await self.kb.get_document_chunks(doc_id)
        if chunks:
            # 优先从 chunk metadata 中获取 source_path
            source_path = chunks[0]["metadata"].get("source_path", "")

        # 3. 从 SourceStore 读取文档内容
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
                # 如果无法读取源文件，从 chunks 重建内容
                content = "\n\n".join(
                    ch.get("content", "") for ch in chunks
                )

        # 4. 构建 chunks 列表
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

        # 5. 处理 tags：Redis 索引中可能是逗号分隔字符串或列表
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

    async def rename_directory(self, old_path: str, new_path: str) -> dict:
        """重命名目录：移动所有子文档（需写锁保护），同步更新空目录记录"""
        old_path = DirectoryTree.validate_path(old_path)
        new_path = DirectoryTree.validate_path(new_path)
        if not old_path:
            raise HTTPException(status_code=400, detail="不能重命名根目录")
        if old_path == new_path:
            return {"success": True, "moved": 0}

        async with self.write_lock:
            # 从 Redis 索引获取所有文档，按路径前缀匹配
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

            # 同步更新 directories.json 中的目录记录
            dirs = _load_dirs()
            dirs = [new_path if d == old_path or d.startswith(old_path + "/") else d for d in dirs]
            # 对于以 old_path 开头的子目录，替换前缀
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
            # 从 Redis 索引获取所有文档，按路径前缀匹配
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

            # 从 directories.json 移除被删目录及其子目录
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
        # 规范化换行：浏览器表单提交可能携带 \r\n
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
        content_size = _content_size_kb(content)

        # 1. 保存源文件到 MinIO/本地
        source_path = self.source_store.save_source(doc_id, content, path)

        # 2. 切片（在锁外完成耗时操作）
        chunks = chunk_markdown(content, self.settings.CHUNK_SIZE, self.settings.CHUNK_OVERLAP)
        if not chunks:
            raise HTTPException(
                status_code=400,
                detail=f"文档「{title}」(doc_id={doc_id}) 内容无法切片。"
                f"内容大小: {content_size}，可能全部为空白字符，请检查内容",
            )

        # 3. 生成 Embedding（在锁外完成）
        try:
            embeddings = await self.embedder.embed(chunks)
        except EmbeddingError as e:
            raise HTTPException(
                status_code=503,
                detail=f"文档「{title}」(doc_id={doc_id}) Embedding 生成失败。"
                f"切片数: {len(chunks)}，内容大小: {content_size}。\n{e}",
            )
        if not embeddings or len(embeddings) != len(chunks):
            raise HTTPException(
                status_code=503,
                detail=f"文档「{title}」(doc_id={doc_id}) Embedding 生成失败。"
                f"期望 {len(chunks)} 个向量，实际收到 {len(embeddings) if embeddings else 0} 个。\n"
                f"可能原因: Ollama 服务异常或模型 {self.settings.OLLAMA_MODEL} 未正确加载",
            )

        # 4. 缓存内容哈希到 Redis（用于后续变更检测）
        content_hash = _content_hash(content)

        # 5. 获取写锁并写入 Chroma
        try:
            async with self.write_lock:
                # 注：新建文档无需 mark_doc_updating，因为 doc_id 尚未被索引，
                #     在 add_document_chunks 完成前搜索不可见，无中间态问题
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
                # 写入成功后缓存内容哈希
                await self.kb.set_doc_content_hash(doc_id, content_hash)
                logger.info(
                    f"Document imported: doc_id={doc_id}, title={title}, "
                    f"path={path}, chunks={len(chunks)}, "
                    f"size={content_size}, created_by={created_by}"
                )
        except WriteLockError:
            logger.warning(
                f"Write lock busy when importing document: "
                f"title={title}, size={content_size}"
            )
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
            raise HTTPException(
                status_code=400,
                detail=f"文档 (doc_id={doc_id}) 标题不能为空，请提供有效的标题",
            )

        tags = tags or []
        new_path = DirectoryTree.validate_path(path)
        now = datetime.now(timezone.utc).isoformat()
        content_size = _content_size_kb(content)

        # 查找旧文档信息
        old_chunks = await self.kb.get_document_chunks(doc_id)
        if not old_chunks:
            raise HTTPException(
                status_code=404,
                detail=f"文档 (doc_id={doc_id}) 不存在，可能已被删除。请使用 add_document 添加新文档",
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

        # 0. 变更检测：内容/标题/路径/标签 全部相同时跳过全流程
        changeless = False
        if new_path == old_path and title == old_title and sorted(tags) == sorted(old_tags):
            # 优先从 Redis 缓存读取内容哈希（无 I/O），回退到读源文件
            new_content_hash = _content_hash(content)
            old_content_hash = await self.kb.get_doc_content_hash(doc_id)

            if old_content_hash and new_content_hash == old_content_hash:
                changeless = True
            elif not old_content_hash:
                # Redis 无缓存：回退到读源文件（如旧版本导入的文档）
                logger.debug(
                    f"Content hash not cached for doc_id={doc_id}, "
                    f"falling back to source file read"
                )
                try:
                    old_source_path = old_meta.get("source_path", "")
                    if old_source_path:
                        old_content = self.source_store.get_source_by_full_path(old_source_path) or ""
                    else:
                        old_content = self.source_store.get_source(doc_id, old_path) or ""
                    if old_content and new_content_hash == _content_hash(old_content):
                        changeless = True
                except Exception:
                    pass  # 无法读取旧内容时跳过检测，继续正常更新

        if changeless:
            logger.info(
                f"Document unchanged, skip update: doc_id={doc_id}, title={title}, "
                f"chunks={len(old_chunks)}, size={content_size}"
            )
            return {
                "success": True,
                "doc_id": doc_id,
                "message": "内容无变化，已跳过更新",
                "skipped": True,
                "chunks": len(old_chunks),
            }

        # 1. 保存新源文件到 MinIO/本地
        if old_path and old_path != new_path:
            # 路径变更：移动源文件
            self.source_store.move_source(doc_id, old_path, new_path)
        source_path = self.source_store.save_source(doc_id, content, new_path)

        # 2. 重新切片和 Embedding（锁外）
        chunks = chunk_markdown(content, self.settings.CHUNK_SIZE, self.settings.CHUNK_OVERLAP)
        if not chunks:
            raise HTTPException(
                status_code=400,
                detail=f"文档「{title}」(doc_id={doc_id}) 更新内容无法切片。"
                f"内容大小: {content_size}，旧切片数: {len(old_chunks)}",
            )

        try:
            embeddings = await self.embedder.embed(chunks)
        except EmbeddingError as e:
            raise HTTPException(
                status_code=503,
                detail=f"文档「{title}」(doc_id={doc_id}) Embedding 生成失败。"
                f"新切片数: {len(chunks)}，旧切片数: {len(old_chunks)}，"
                f"内容大小: {content_size}。\n{e}",
            )
        if not embeddings or len(embeddings) != len(chunks):
            raise HTTPException(
                status_code=503,
                detail=f"文档「{title}」(doc_id={doc_id}) Embedding 生成失败。"
                f"期望 {len(chunks)} 个向量，实际收到 {len(embeddings) if embeddings else 0} 个。"
                f"旧切片数: {len(old_chunks)}，内容大小: {content_size}",
            )

        # 3. 缓存新内容哈希
        new_content_hash = _content_hash(content)

        # 4. 获取写锁，删除旧切片，写入新切片
        try:
            async with self.write_lock:
                # 标记文档为更新中，列表时暂时跳过
                await self.kb.mark_doc_updating(doc_id)
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
                # 写入成功后缓存内容哈希
                await self.kb.set_doc_content_hash(doc_id, new_content_hash)
                logger.info(
                    f"Document updated: doc_id={doc_id}, title={title}, "
                    f"path={new_path}, chunks={len(chunks)} (was {len(old_chunks)}), "
                    f"size={content_size}, updated_by={updated_by}"
                )
        except WriteLockError:
            logger.warning(
                f"Write lock busy when updating document: "
                f"doc_id={doc_id}, title={title}"
            )
            raise HTTPException(
                status_code=423,
                detail=f"知识库写入锁被占用，文档「{title}」(doc_id={doc_id}) 暂时无法更新，请稍后重试",
            )

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
            raise HTTPException(
                status_code=404,
                detail=f"文档 (doc_id={doc_id}) 不存在，可能已被删除",
            )

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
                logger.info(
                    f"Document deleted: doc_id={doc_id}, title={title}, "
                    f"path={path}, chunks={len(chunks)}, deleted_by={deleted_by}"
                )
        except WriteLockError:
            logger.warning(
                f"Write lock busy when deleting document: "
                f"doc_id={doc_id}, title={title}"
            )
            raise HTTPException(
                status_code=423,
                detail=f"知识库写入锁被占用，文档「{title}」(doc_id={doc_id}) 暂时无法删除，请稍后重试",
            )

        return {
            "success": True,
            "doc_id": doc_id,
            "message": "文档删除成功",
        }

    async def reindex_document(self, doc_id: str) -> dict:
        """重新切片 & 向量化单个文档"""
        chunks = await self.kb.get_document_chunks(doc_id)
        if not chunks:
            raise HTTPException(
                status_code=404,
                detail=f"文档 (doc_id={doc_id}) 不存在，无法重建索引。请确认文档未被删除",
            )

        meta = chunks[0]["metadata"]
        title = meta.get("title", "")
        path = meta.get("path", "")
        source_path = meta.get("source_path", "")
        tags = (
            meta.get("tags", "").split(",")
            if isinstance(meta.get("tags"), str)
            else (meta.get("tags") or [])
        )

        # 从 MinIO 读源文件
        try:
            if source_path:
                content = self.source_store.get_source_by_full_path(source_path)
            else:
                content = self.source_store.get_source(doc_id, path)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"读取文档「{title}」(doc_id={doc_id}) 源文件失败: {e}。"
                f"源文件路径: {source_path or f'documents/{path}/{doc_id}/source.md'}。"
                f"请检查 MinIO 服务是否正常",
            )
        if not content or not content.strip():
            raise HTTPException(
                status_code=400,
                detail=f"文档「{title}」(doc_id={doc_id}) 源文件内容为空。"
                f"旧切片数: {len(chunks)}。请检查源文件是否被意外清空",
            )

        content_size = _content_size_kb(content)

        # 重新切片
        new_chunks = chunk_markdown(content, self.settings.CHUNK_SIZE, self.settings.CHUNK_OVERLAP)
        if not new_chunks:
            raise HTTPException(
                status_code=400,
                detail=f"文档「{title}」(doc_id={doc_id}) 重新切片结果为空。"
                f"内容大小: {content_size}，旧切片数: {len(chunks)}",
            )

        # 生成 Embedding
        try:
            embeddings = await self.embedder.embed(new_chunks)
        except EmbeddingError as e:
            raise HTTPException(
                status_code=503,
                detail=f"文档「{title}」(doc_id={doc_id}) 重建索引 Embedding 失败。"
                f"新切片数: {len(new_chunks)}，内容大小: {content_size}。\n{e}",
            )
        if not embeddings or len(embeddings) != len(new_chunks):
            raise HTTPException(
                status_code=503,
                detail=f"文档「{title}」(doc_id={doc_id}) 重建索引 Embedding 失败。"
                f"期望 {len(new_chunks)} 个向量，实际收到 {len(embeddings) if embeddings else 0} 个。"
                f"旧切片数: {len(chunks)}，新切片数: {len(new_chunks)}",
            )

        # 缓存新内容哈希
        new_content_hash = _content_hash(content)

        # 写锁保护下，删除旧切片 + 写入新切片
        try:
            async with self.write_lock:
                # 标记文档为更新中
                await self.kb.mark_doc_updating(doc_id)
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
                # 写入成功后缓存内容哈希
                await self.kb.set_doc_content_hash(doc_id, new_content_hash)
        except WriteLockError:
            raise HTTPException(
                status_code=423,
                detail=f"知识库写入锁被占用，文档「{title}」(doc_id={doc_id}) 暂时无法重建索引，请稍后重试",
            )

        logger.info(
            f"Document reindexed: doc_id={doc_id}, title={title}, "
            f"chunks_old={len(chunks)}, chunks_new={len(new_chunks)}, "
            f"size={content_size}"
        )
        return {
            "success": True,
            "doc_id": doc_id,
            "chunks_old": len(chunks),
            "chunks_new": len(new_chunks),
        }

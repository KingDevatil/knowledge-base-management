"""KnowledgeTools — write operations for knowledge base document management."""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from config import get_settings
from document_metadata import (
    extract_document_header_metadata,
    metadata_override_enabled,
    merge_metadata_values,
    metadata_value_key,
    normalize_metadata_values,
)
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
from ingestion import DocumentIngestionPipeline, IngestionResult, ProgressCallback
from document_versions import DocumentVersionStore
from audit_log import AuditLogger

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
        self.ingestion_tasks: dict[str, dict] = {}
        self.ingestion_task_payloads: dict[str, dict] = {}
        self.cleanup_tasks: dict[str, dict] = {}
        self.version_store = DocumentVersionStore(self.settings.KBDATA_DIR or "kbdata")
        self.audit_logger = AuditLogger(self.settings.KBDATA_DIR or "kbdata")

    @staticmethod
    def _write_lock_conflict(message: str, error: WriteLockError) -> HTTPException:
        return HTTPException(
            status_code=423,
            detail={
                "message": message,
                "retry_after_ms": error.retry_after_ms,
            },
        )

    @staticmethod
    async def _notify_progress(
        progress_callback: ProgressCallback | None,
        progress: float,
        message: str,
    ) -> None:
        if progress_callback is None:
            return
        try:
            await progress_callback(progress, message)
        except Exception:
            return

    @staticmethod
    def _metadata_override_values(metadata: dict) -> tuple[bool, list[str], list[str]]:
        """Return the durable, independently edited tags/entities when present."""

        if not metadata_override_enabled(metadata.get("metadata_overridden")):
            return False, [], []

        raw_tags = metadata.get("tags_override")
        raw_entities = metadata.get("entities_override")
        return (
            True,
            normalize_metadata_values(
                metadata.get("tags", []) if raw_tags is None else raw_tags
            ),
            normalize_metadata_values(
                metadata.get("entities", []) if raw_entities is None else raw_entities
            ),
        )

    # ---------- 目录管理（需写锁）----------

    async def _snapshot_document_version(self, doc_id: str, reason: str, created_by: str = "system") -> dict | None:
        try:
            detail = await self.get_document(doc_id)
        except Exception:
            return None
        return self.version_store.save_version(
            doc_id=doc_id,
            title=detail.get("title", ""),
            content=detail.get("content", ""),
            path=detail.get("path", ""),
            tags=detail.get("tags", []),
            created_by=created_by,
            reason=reason,
        )

    async def _move_document_path_locked(
        self,
        doc_id: str,
        old_path: str,
        new_path: str,
        chunks: list[dict],
        index_document: dict,
        updated_by: str,
        updated_at: str,
    ) -> str:
        """Move source and path metadata without changing chunks or embeddings.

        The caller must hold ``self.write_lock``.
        """
        old_metadatas = [dict(chunk.get("metadata", {})) for chunk in chunks]
        old_index = dict(index_document or {})
        new_source_path = self.source_store.move_source(doc_id, old_path, new_path)
        new_metadatas = [{
            **metadata,
            "path": new_path,
            "source_path": new_source_path,
            "updated_at": updated_at,
            "updated_by": updated_by,
        } for metadata in old_metadatas]
        chunk_ids = [chunk["id"] for chunk in chunks]
        new_index = {
            **old_index,
            "doc_id": doc_id,
            "path": new_path,
            "updated_at": updated_at,
        }

        try:
            self.kb.collection.update(ids=chunk_ids, metadatas=new_metadatas)
            await self.kb._doc_index_set(doc_id, new_index)
        except Exception:
            logger.exception("Document path update failed, restoring old path: doc_id=%s", doc_id)
            try:
                self.kb.collection.update(ids=chunk_ids, metadatas=old_metadatas)
                await self.kb._doc_index_set(doc_id, old_index)
                self.source_store.move_source(doc_id, new_path, old_path)
            except Exception:
                logger.exception("Document path rollback failed: doc_id=%s", doc_id)
            raise

        return new_source_path

    async def rename_directory(self, old_path: str, new_path: str) -> dict:
        """重命名目录：移动所有子文档（需写锁保护），同步更新空目录记录"""
        old_path = DirectoryTree.validate_path(old_path)
        new_path = DirectoryTree.validate_path(new_path)
        if not old_path:
            raise HTTPException(status_code=400, detail="不能重命名根目录")
        if old_path == new_path:
            return {"success": True, "moved": 0}
        if new_path.startswith(old_path + "/"):
            raise HTTPException(status_code=400, detail="目录不能移动到自身的子目录")

        async with self.write_lock:
            all_docs = await self.kb._doc_index_all()
            dirs = _load_dirs()
            existing_paths = set(dirs)
            for doc in all_docs:
                parts = DirectoryTree.validate_path(doc.get("path", "")).split("/")
                existing_paths.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
            paths_outside_source = {
                path for path in existing_paths
                if path != old_path and not path.startswith(old_path + "/")
            }
            if new_path in paths_outside_source:
                raise HTTPException(status_code=409, detail="目标目录已存在")

            matching_docs = [
                d for d in all_docs
                if d.get("path", "") == old_path or d.get("path", "").startswith(old_path + "/")
            ]

            moved = 0
            for doc in matching_docs:
                doc_id = doc.get("doc_id", "")
                doc_path = doc.get("path", "")
                new_doc_path = new_path if doc_path == old_path else new_path + doc_path[len(old_path):]
                chunks = await self.kb.get_document_chunks(doc_id)
                await self._move_document_path_locked(
                    doc_id, doc_path, new_doc_path, chunks, doc,
                    updated_by="directory_rename",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                moved += 1

            new_dirs = []
            for d in dirs:
                if d == old_path:
                    new_dirs.append(new_path)
                elif d.startswith(old_path + "/"):
                    new_dirs.append(new_path + d[len(old_path):])
                else:
                    new_dirs.append(d)
            _save_dirs(new_dirs)

        await self.refresh_keyword_index_safely("rename_directory")
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
                chunks = await self.kb.get_document_chunks(doc_id)
                await self._move_document_path_locked(
                    doc_id, doc_path, "", chunks, doc,
                    updated_by="directory_delete",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                moved += 1

            dirs = _load_dirs()
            dirs = [d for d in dirs if d != path and not d.startswith(path + "/")]
            _save_dirs(dirs)

        await self.refresh_keyword_index_safely("delete_directory")
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
        progress_callback: ProgressCallback | None = None,
    ) -> IngestionResult:
        """内部方法：通过入库 Pipeline 导入文档。"""
        payload = {
            "title": title,
            "content": content,
            "path": path,
            "tags": list(tags or []),
            "created_by": created_by,
            "doc_id": doc_id,
        }
        pipeline = DocumentIngestionPipeline(
            kb=self.kb,
            source_store=self.source_store,
            embedder=self.embedder,
            write_lock=self.write_lock,
            chunk_size=self.settings.CHUNK_SIZE,
            chunk_overlap=self.settings.CHUNK_OVERLAP,
        )
        try:
            result = await pipeline.import_document(
                title=title,
                content=content,
                path=path,
                tags=tags,
                created_by=created_by,
                doc_id=doc_id,
                progress_callback=progress_callback,
            )
        except WriteLockError as exc:
            if pipeline.latest_task:
                self.ingestion_tasks[pipeline.latest_task.task_id] = pipeline.latest_task.to_dict()
                self.ingestion_task_payloads[pipeline.latest_task.task_id] = {
                    **payload,
                    "doc_id": pipeline.latest_task.doc_id,
                }
            logger.warning(f"Write lock busy when importing: title={title}, size={content_size_kb(content)}")
            raise self._write_lock_conflict(
                f"知识库写入锁被占用，文档「{title}」暂时无法导入，请稍后重试",
                exc,
            )
        except Exception:
            if pipeline.latest_task:
                self.ingestion_tasks[pipeline.latest_task.task_id] = pipeline.latest_task.to_dict()
                self.ingestion_task_payloads[pipeline.latest_task.task_id] = {
                    **payload,
                    "doc_id": pipeline.latest_task.doc_id,
                }
            raise

        self.ingestion_tasks[result.task.task_id] = result.task.to_dict()
        self.ingestion_task_payloads[result.task.task_id] = {
            **payload,
            "doc_id": result.doc_id,
        }
        logger.info(
            f"Document imported: doc_id={result.doc_id}, title={title}, "
            f"path={path}, chunks={result.chunks}, task_id={result.task.task_id}"
        )
        return result

    async def add_document(
        self,
        title: str,
        content: str,
        path: str = "",
        tags: list[str] | None = None,
        created_by: str = "system",
        progress_callback: ProgressCallback | None = None,
    ) -> dict:
        """添加新文档"""
        tags = tags or []
        result = await self._import_document(
            title,
            content,
            path,
            tags,
            created_by,
            progress_callback=progress_callback,
        )
        await self.refresh_keyword_document_safely(result.doc_id, "add_document")
        return {
            "success": True,
            "doc_id": result.doc_id,
            "task_id": result.task.task_id,
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
        """导入 Markdown 内容（委托给 add_document）"""
        return await self.add_document(title, markdown_content, path, tags, created_by)

    async def retry_ingestion_task(self, task_id: str, retried_by: str = "system") -> dict:
        """Retry a failed ingestion task using the original import payload."""
        task = self.ingestion_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"入库任务不存在: {task_id}")
        if task.get("status") != "failed":
            raise HTTPException(status_code=400, detail=f"只能重试失败任务: {task_id}")

        payload = self.ingestion_task_payloads.get(task_id)
        if not payload:
            raise HTTPException(status_code=409, detail=f"入库任务缺少可重试载荷: {task_id}")

        result = await self._import_document(
            title=payload["title"],
            content=payload["content"],
            path=payload.get("path", ""),
            tags=payload.get("tags", []),
            created_by=retried_by or payload.get("created_by", "system"),
            doc_id=payload.get("doc_id"),
        )
        await self.refresh_keyword_document_safely(result.doc_id, "retry_ingestion_task")
        retry_task = result.task.to_dict()
        retry_task["retried_from"] = task_id
        self.ingestion_tasks[result.task.task_id] = retry_task
        task["retry_task_id"] = result.task.task_id

        return {
            "success": True,
            "doc_id": result.doc_id,
            "task_id": result.task.task_id,
            "retried_from": task_id,
            "message": "入库任务重试成功",
        }

    async def _restore_document_chunks(
        self,
        doc_id: str,
        title: str,
        old_chunks: list[dict],
        content_hash_value: str | None = None,
    ) -> None:
        """Restore previously deleted chunks after a failed replacement write."""
        if not old_chunks:
            return

        old_embeddings = [ch.get("embedding") for ch in old_chunks]
        if any(emb is None for emb in old_embeddings):
            logger.error(f"Cannot restore document without old embeddings: doc_id={doc_id}")
            return

        old_metadata = dict(old_chunks[0].get("metadata", {}))
        await self.kb.add_document_chunks(
            doc_id=doc_id,
            title=title or old_metadata.get("title", ""),
            chunks=[ch.get("content", "") for ch in old_chunks],
            embeddings=old_embeddings,
            metadata={
                "path": old_metadata.get("path", ""),
                "tags": old_metadata.get("tags", ""),
                "header_tags": old_metadata.get("header_tags", ""),
                "entities": old_metadata.get("entities", ""),
                "header_entities": old_metadata.get("header_entities", ""),
                "metadata_overridden": old_metadata.get("metadata_overridden", False),
                "tags_override": old_metadata.get("tags_override", ""),
                "entities_override": old_metadata.get("entities_override", ""),
                "source_path": old_metadata.get("source_path", ""),
                "source_format": old_metadata.get("source_format", "markdown"),
                "created_at": old_metadata.get("created_at", ""),
                "updated_at": old_metadata.get("updated_at", ""),
                "created_by": old_metadata.get("created_by", "system"),
                "updated_by": old_metadata.get("updated_by", "system"),
            },
        )
        if content_hash_value:
            await self.kb.set_doc_content_hash(doc_id, content_hash_value)

    def _add_staging_chunks(
        self,
        staging_doc_id: str,
        target_doc_id: str,
        title: str,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict,
    ) -> None:
        """Write staging chunks directly to Chroma without exposing them in Redis doc index."""
        ids = [f"{staging_doc_id}#chunk-{i}" for i in range(len(chunks))]
        metadatas = []
        for i in range(len(chunks)):
            item = {
                **metadata,
                "doc_id": staging_doc_id,
                "target_doc_id": target_doc_id,
                "title": title,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "__write_status": "staging",
            }
            for field in (
                "tags",
                "header_tags",
                "entities",
                "header_entities",
                "tags_override",
                "entities_override",
            ):
                if field in item and isinstance(item[field], list):
                    item[field] = ",".join(item[field])
            metadatas.append(item)

        self.kb.collection.add(
            ids=ids,
            documents=chunks,
            embeddings=embeddings,
            metadatas=metadatas,
        )

    def _delete_staging_chunks(self, staging_doc_id: str) -> None:
        try:
            results = self.kb.collection.get(where={"doc_id": staging_doc_id})
            ids = results.get("ids", []) if isinstance(results, dict) else []
            if ids:
                self.kb.collection.delete(ids=ids)
        except Exception:
            logger.warning(f"Failed to cleanup staging chunks: staging_doc_id={staging_doc_id}")
            self._record_cleanup_task(
                cleanup_type="staging_chunks",
                target=staging_doc_id,
                payload={"staging_doc_id": staging_doc_id},
            )

    def _cleanup_staging_source(self, source_path: str) -> None:
        try:
            self.source_store.delete_source_by_path(source_path)
        except Exception as exc:
            logger.warning(f"Failed to cleanup staging source: {source_path}")
            self._record_cleanup_task(
                cleanup_type="staging_source",
                target=source_path,
                payload={"source_path": source_path},
                error=str(exc),
            )

    def _record_cleanup_task(
        self,
        cleanup_type: str,
        target: str,
        payload: dict,
        error: str = "",
    ) -> str:
        task_id = f"cleanup-{uuid.uuid4().hex}"
        self.cleanup_tasks[task_id] = {
            "task_id": task_id,
            "type": cleanup_type,
            "target": target,
            "payload": payload,
            "status": "pending",
            "error": error,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return task_id

    def retry_cleanup_task(self, task_id: str) -> dict:
        task = self.cleanup_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"清理任务不存在: {task_id}")
        try:
            if task["type"] == "staging_chunks":
                staging_doc_id = task["payload"]["staging_doc_id"]
                results = self.kb.collection.get(where={"doc_id": staging_doc_id})
                ids = results.get("ids", []) if isinstance(results, dict) else []
                if ids:
                    self.kb.collection.delete(ids=ids)
            elif task["type"] == "staging_source":
                self.source_store.delete_source_by_path(task["payload"]["source_path"])
            else:
                raise HTTPException(status_code=400, detail=f"未知清理任务类型: {task['type']}")
        except Exception as exc:
            task["status"] = "failed"
            task["error"] = str(exc)
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            raise
        task["status"] = "succeeded"
        task["error"] = ""
        task["updated_at"] = datetime.now(timezone.utc).isoformat()
        return {"success": True, "task_id": task_id, "status": task["status"]}

    async def update_document(
        self,
        doc_id: str,
        title: str,
        content: str,
        path: str = "",
        tags: list[str] | None = None,
        updated_by: str = "system",
        progress_callback: ProgressCallback | None = None,
        path_explicit: bool = False,
    ) -> dict:
        """更新已有文档"""
        if not doc_id:
            raise HTTPException(status_code=400, detail="文档 ID 不能为空")
        if not title or not title.strip():
            raise HTTPException(
                status_code=400,
                detail=f"文档 (doc_id={doc_id}) 标题不能为空",
            )

        await self._notify_progress(progress_callback, 5, "读取现有文档")
        header_metadata = extract_document_header_metadata(content)
        tags = merge_metadata_values(tags or [], header_metadata.tags)
        header_tags = header_metadata.tags
        entities = header_metadata.entities
        header_entities = header_metadata.entities
        await self._snapshot_document_version(doc_id, "before_update", updated_by)
        now = datetime.now(timezone.utc).isoformat()
        size_label = content_size_kb(content)

        old_chunks = await self.kb.get_document_chunks(doc_id, include_embeddings=True)
        if not old_chunks:
            raise HTTPException(
                status_code=404,
                detail=f"文档 (doc_id={doc_id}) 不存在，可能已被删除",
            )

        old_meta = old_chunks[0]["metadata"] if old_chunks else {}
        old_path = old_meta.get("path", "")
        old_title = old_meta.get("title", "")
        old_tags = normalize_metadata_values(old_meta.get("tags", []))
        metadata_overridden, tags_override, entities_override = self._metadata_override_values(old_meta)
        if metadata_overridden:
            tags = tags_override
            entities = entities_override

        # 未传入新路径/标签时保留原值，避免 Agent 遗漏参数导致数据丢失
        new_path = DirectoryTree.validate_path(path) if (path or path_explicit) else old_path

        # 变更检测
        new_c_hash = content_hash(content)
        old_c_hash = await self.kb.get_doc_content_hash(doc_id)
        old_source_content = ""
        content_unchanged = bool(old_c_hash and new_c_hash == old_c_hash)
        if not content_unchanged:
            old_source_content = self._read_source_content(
                doc_id,
                old_path,
                old_meta.get("source_path", ""),
            )
            content_unchanged = bool(
                old_source_content and new_c_hash == content_hash(old_source_content)
            )

        metadata_unchanged = title == old_title and sorted(tags) == sorted(old_tags)
        changeless = new_path == old_path and metadata_unchanged and content_unchanged

        if changeless:
            logger.info(f"Document unchanged, skip: doc_id={doc_id}, title={title}")
            await self._notify_progress(progress_callback, 100, "文档内容无变化")
            return {
                "success": True, "doc_id": doc_id,
                "message": "内容无变化，已跳过更新",
                "skipped": True, "chunks": len(old_chunks),
            }

        if new_path != old_path and metadata_unchanged and content_unchanged:
            await self._notify_progress(progress_callback, 50, "仅更新文档目录")
            try:
                await self._notify_progress(progress_callback, 65, "等待写入锁")
                async with self.write_lock:
                    current_index = await self.kb._doc_index_get(doc_id) or {
                        "doc_id": doc_id,
                        "title": old_title,
                        "path": old_path,
                        "tags": old_tags,
                        "chunk_count": len(old_chunks),
                    }
                    await self._move_document_path_locked(
                        doc_id, old_path, new_path, old_chunks, current_index,
                        updated_by=updated_by,
                        updated_at=now,
                    )
            except WriteLockError as exc:
                raise self._write_lock_conflict(
                    f"写入锁被占用，文档「{title}」暂时无法移动，请稍后重试",
                    exc,
                )

            await self.refresh_keyword_document_safely(doc_id, "move_document_path")
            await self._notify_progress(progress_callback, 100, "文档目录更新完成")
            logger.info(
                "Document path updated without re-embedding: doc_id=%s, old_path=%s, new_path=%s",
                doc_id,
                old_path,
                new_path,
            )
            return {
                "success": True,
                "doc_id": doc_id,
                "path": new_path,
                "path_only": True,
                "skipped_embedding": True,
                "graph_rebuild_required": True,
                "chunks": len(old_chunks),
                "message": "文档目录已更新，正文、切片和向量保持不变；知识图谱需要重建。",
            }

        old_source_path = old_meta.get("source_path", "")
        if not old_source_content:
            old_source_content = self._read_source_content(doc_id, old_path, old_source_path)

        chunks = chunk_markdown(content, self.settings.CHUNK_SIZE, self.settings.CHUNK_OVERLAP)
        if not chunks:
            raise HTTPException(
                status_code=400,
                detail=f"文档「{title}」(doc_id={doc_id}) 更新内容无法切片。"
                f"内容大小: {size_label}",
            )

        await self._notify_progress(progress_callback, 35, "生成向量")
        try:
            embeddings = await self.embedder.embed(chunks)
        except EmbeddingError as e:
            raise HTTPException(
                status_code=503,
                detail=f"文档「{title}」(doc_id={doc_id}) Embedding 失败。\n{e}",
            )

        new_c_hash = content_hash(content)
        restore_hash = await self.kb.get_doc_content_hash(doc_id)
        source_path = ""
        staging_doc_id = f"{doc_id}__staging__{uuid.uuid4().hex}"
        staging_source_path = ""
        deleted_old = False

        try:
            await self._notify_progress(progress_callback, 55, "准备暂存数据")
            staging_source_path = self.source_store.save_source(staging_doc_id, content, new_path)
            self._add_staging_chunks(
                staging_doc_id=staging_doc_id,
                target_doc_id=doc_id,
                title=title,
                chunks=chunks,
                embeddings=embeddings,
                metadata={
                    "path": new_path,
                    "tags": tags,
                    "header_tags": header_tags,
                    "entities": entities,
                    "header_entities": header_entities,
                    "metadata_overridden": metadata_overridden,
                    "tags_override": tags_override,
                    "entities_override": entities_override,
                    "source_path": staging_source_path,
                    "source_format": "markdown",
                    "created_at": old_meta.get("created_at", now),
                    "updated_at": now,
                    "created_by": old_meta.get("created_by", updated_by),
                    "updated_by": updated_by,
                },
            )

            await self._notify_progress(progress_callback, 65, "等待写入锁")
            async with self.write_lock:
                await self._notify_progress(progress_callback, 75, "替换文档索引")
                try:
                    if old_path != new_path:
                        self.source_store.move_source(doc_id, old_path, new_path)
                    source_path = self.source_store.save_source(doc_id, content, new_path)
                    await self.kb.mark_doc_updating(doc_id)
                    await self.kb.delete_document(doc_id)
                    deleted_old = True
                    metadata = {
                        "path": new_path,
                        "tags": tags,
                        "header_tags": header_tags,
                        "entities": entities,
                        "header_entities": header_entities,
                        "metadata_overridden": metadata_overridden,
                        "tags_override": tags_override,
                        "entities_override": entities_override,
                        "source_path": source_path,
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
                except Exception:
                    logger.exception(f"Document update write failed, restoring previous chunks: doc_id={doc_id}")
                    try:
                        if old_source_content:
                            self.source_store.save_source(doc_id, old_source_content, old_path)
                        if old_path != new_path and source_path:
                            try:
                                self.source_store.delete_source_by_path(source_path)
                            except Exception:
                                logger.warning(f"Failed to delete failed update source copy: {source_path}")
                        if deleted_old:
                            await self._restore_document_chunks(doc_id, old_title, old_chunks, restore_hash)
                    except Exception:
                        logger.exception(f"Document restore failed after update write failure: doc_id={doc_id}")
                    raise
                logger.info(
                    f"Document updated: doc_id={doc_id}, title={title}, "
                    f"path={new_path}, chunks={len(chunks)} (was {len(old_chunks)})"
                )
        except WriteLockError as exc:
            raise self._write_lock_conflict(
                f"写入锁被占用，文档「{title}」暂时无法更新，请稍后重试",
                exc,
            )
        finally:
            self._delete_staging_chunks(staging_doc_id)
            if staging_source_path:
                self._cleanup_staging_source(staging_source_path)

        await self.refresh_keyword_document_safely(doc_id, "update_document")
        await self._notify_progress(progress_callback, 100, "文档更新完成")

        return {"success": True, "doc_id": doc_id, "message": "文档更新成功"}

    async def update_document_metadata(
        self,
        doc_id: str,
        tags: list[str] | str | None = None,
        entities: list[str] | str | None = None,
        updated_by: str = "system",
    ) -> dict:
        """Update retrieval/graph metadata without changing document source or chunks.

        The resulting values are stored as a durable manual override. Reindexing
        refreshes the source-derived header metadata for reference but keeps this
        override active, so a graph rebuilt afterwards sees the edited values.
        """

        if not doc_id:
            raise HTTPException(status_code=400, detail="文档 ID 不能为空")

        normalized_tags = normalize_metadata_values(tags or [])
        normalized_entities = normalize_metadata_values(entities or [])
        chunks = await self.kb.get_document_chunks(doc_id)
        if not chunks:
            raise HTTPException(status_code=404, detail=f"文档 (doc_id={doc_id}) 不存在")

        now = datetime.now(timezone.utc).isoformat()
        old_metadatas = [dict(chunk.get("metadata", {})) for chunk in chunks]
        new_metadatas: list[dict] = []
        for metadata in old_metadatas:
            new_metadatas.append({
                **metadata,
                "tags": ",".join(normalized_tags),
                "entities": ",".join(normalized_entities),
                "metadata_overridden": True,
                "tags_override": ",".join(normalized_tags),
                "entities_override": ",".join(normalized_entities),
                "updated_at": now,
                "updated_by": updated_by,
            })

        try:
            async with self.write_lock:
                chunk_ids = [chunk["id"] for chunk in chunks]
                try:
                    self.kb.collection.update(ids=chunk_ids, metadatas=new_metadatas)

                    current = await self.kb._doc_index_get(doc_id) or {}
                    updated_index = {
                        **current,
                        "doc_id": doc_id,
                        "title": current.get("title", old_metadatas[0].get("title", "")),
                        "path": current.get("path", old_metadatas[0].get("path", "")),
                        "tags": normalized_tags,
                        "header_tags": normalize_metadata_values(
                            current.get("header_tags", old_metadatas[0].get("header_tags", []))
                        ),
                        "entities": normalized_entities,
                        "header_entities": normalize_metadata_values(
                            current.get("header_entities", old_metadatas[0].get("header_entities", []))
                        ),
                        "metadata_overridden": True,
                        "tags_override": normalized_tags,
                        "entities_override": normalized_entities,
                        "chunk_count": current.get("chunk_count", len(chunks)),
                        "created_at": current.get("created_at", old_metadatas[0].get("created_at", "")),
                        "updated_at": now,
                    }
                    await self.kb._doc_index_set(doc_id, updated_index)
                except Exception:
                    try:
                        self.kb.collection.update(ids=chunk_ids, metadatas=old_metadatas)
                    except Exception:
                        logger.exception(
                            "Failed to restore metadata after metadata update failure: doc_id=%s",
                            doc_id,
                        )
                    raise
        except WriteLockError as exc:
            raise self._write_lock_conflict(
                "写入锁被占用，文档元数据暂时无法更新，请稍后重试",
                exc,
            )

        await self.refresh_keyword_document_safely(doc_id, "update_document_metadata")
        logger.info("Document metadata updated: doc_id=%s", doc_id)
        return {
            "success": True,
            "doc_id": doc_id,
            "tags": normalized_tags,
            "entities": normalized_entities,
            "metadata_overridden": True,
            "graph_rebuild_required": True,
            "updated_at": now,
            "message": "标签和实体已保存，文档正文未修改；请重建知识图谱以刷新关联。",
        }

    async def delete_document(self, doc_id: str, deleted_by: str = "system") -> dict:
        """删除文档"""
        if not doc_id:
            raise HTTPException(status_code=400, detail="文档 ID 不能为空")

        await self._snapshot_document_version(doc_id, "before_delete", deleted_by)
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
        except WriteLockError as exc:
            raise self._write_lock_conflict(
                f"写入锁被占用，文档「{title}」暂时无法删除",
                exc,
            )

        await self.remove_keyword_document_safely(doc_id, "delete_document")
        return {"success": True, "doc_id": doc_id, "message": "文档删除成功"}

    async def list_document_versions(self, doc_id: str) -> dict:
        if not doc_id:
            raise HTTPException(status_code=400, detail="文档 ID 不能为空")
        return {"doc_id": doc_id, "versions": self.version_store.list_versions(doc_id)}

    async def restore_document_version(self, doc_id: str, version_id: str, restored_by: str = "system") -> dict:
        try:
            version = self.version_store.get_version(doc_id, version_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="文档版本不存在")
        await self._snapshot_document_version(doc_id, "before_restore", restored_by)
        result = await self.update_document(
            doc_id=doc_id,
            title=version.get("title", ""),
            content=version.get("content", ""),
            path=version.get("path", ""),
            tags=version.get("tags", []),
            updated_by=restored_by,
        )
        result["restored_version_id"] = version_id
        return result

    async def find_similar_documents(self, title: str, content: str, path: str = "", top_k: int = 5) -> dict:
        matches: list[dict] = []
        content_sha = content_hash(content)
        docs = await self.kb._doc_index_all()
        for doc in docs:
            reason = ""
            similarity = 0.0
            if path == doc.get("path", "") and title.strip().lower() == str(doc.get("title", "")).strip().lower():
                reason = "title_path"
                similarity = 1.0
            if doc.get("content_hash") == content_sha:
                reason = "content_hash"
                similarity = 1.0
            if similarity > 0:
                matches.append({
                    "doc_id": doc.get("doc_id", ""),
                    "title": doc.get("title", ""),
                    "path": doc.get("path", ""),
                    "similarity": similarity,
                    "reason": reason,
                })
        if content.strip():
            try:
                search = await self.search_knowledge(content[:1000], top_k=top_k, filter_path=path)
                seen = {item["doc_id"] for item in matches}
                for item in search.get("results", []):
                    doc_id = item.get("doc_id", "")
                    if doc_id and doc_id not in seen:
                        seen.add(doc_id)
                        matches.append({
                            "doc_id": doc_id,
                            "title": item.get("title", ""),
                            "path": item.get("path", ""),
                            "similarity": item.get("score", 0),
                            "reason": "semantic",
                        })
            except Exception:
                pass
        matches.sort(key=lambda item: item.get("similarity", 0), reverse=True)
        return {"matches": matches[:top_k], "total": min(len(matches), top_k)}

    async def upsert_document(
        self,
        title: str,
        content: str,
        path: str = "",
        tags: list[str] | None = None,
        match_strategy: str = "title_path",
        on_conflict: str = "update",
        created_by: str = "system",
    ) -> dict:
        similar = await self.find_similar_documents(title, content, path, top_k=5)
        matches = similar.get("matches", [])
        target = None
        for match in matches:
            if match_strategy == "title_path" and match.get("reason") == "title_path":
                target = match
                break
            if match_strategy == "hash" and match.get("reason") == "content_hash":
                target = match
                break
            if match_strategy == "semantic" and float(match.get("similarity", 0)) >= 0.9:
                target = match
                break
        if not target:
            result = await self.add_document(title, content, path, tags or [], created_by)
            result["action"] = "created"
            result["similar_matches"] = matches
            return result
        if on_conflict == "skip":
            return {"success": True, "action": "skipped", "doc_id": target.get("doc_id"), "similar_matches": matches}
        if on_conflict == "create_new":
            result = await self.add_document(title, content, path, tags or [], created_by)
            result["action"] = "created_new"
            result["similar_matches"] = matches
            return result
        result = await self.update_document(target.get("doc_id", ""), title, content, path, tags or [], created_by)
        result["action"] = "updated"
        result["similar_matches"] = matches
        return result

    async def reindex_document(
        self,
        doc_id: str,
        progress_callback: ProgressCallback | None = None,
    ) -> dict:
        """重新切片 & 向量化单个文档"""
        await self._notify_progress(progress_callback, 5, "读取现有文档")
        chunks = await self.kb.get_document_chunks(doc_id, include_embeddings=True)
        if not chunks:
            raise HTTPException(status_code=404, detail=f"文档 (doc_id={doc_id}) 不存在")

        meta = chunks[0]["metadata"]
        title = meta.get("title", "")
        path = meta.get("path", "")
        source_path = meta.get("source_path", "")
        old_tags = normalize_metadata_values(meta.get("tags", []))
        old_header_tags = normalize_metadata_values(meta.get("header_tags", []))
        metadata_overridden, tags_override, entities_override = self._metadata_override_values(meta)

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

        header_metadata = extract_document_header_metadata(content)
        if metadata_overridden:
            tags = tags_override
            entities = entities_override
        elif meta.get("header_tags") is None:
            tags = merge_metadata_values(old_tags, header_metadata.tags)
            entities = header_metadata.entities
        else:
            old_header_keys = {metadata_value_key(tag) for tag in old_header_tags}
            manual_tags = [
                tag for tag in old_tags if metadata_value_key(tag) not in old_header_keys
            ]
            tags = merge_metadata_values(manual_tags, header_metadata.tags)
            entities = header_metadata.entities
        header_tags = header_metadata.tags
        header_entities = header_metadata.entities

        size_label = content_size_kb(content)
        new_chunks = chunk_markdown(content, self.settings.CHUNK_SIZE, self.settings.CHUNK_OVERLAP)
        if not new_chunks:
            raise HTTPException(status_code=400, detail=f"文档「{title}」重新切片结果为空")

        await self._notify_progress(progress_callback, 35, "生成向量")
        try:
            embeddings = await self.embedder.embed(new_chunks)
        except EmbeddingError as e:
            raise HTTPException(status_code=503, detail=f"重建索引 Embedding 失败。\n{e}")

        new_c_hash = content_hash(content)
        restore_hash = await self.kb.get_doc_content_hash(doc_id)
        staging_doc_id = f"{doc_id}__staging__{uuid.uuid4().hex}"
        deleted_old = False

        try:
            await self._notify_progress(progress_callback, 55, "准备暂存数据")
            now = datetime.now(timezone.utc).isoformat()
            staging_metadata = {
                "path": path,
                "tags": tags,
                "header_tags": header_tags,
                "entities": entities,
                "header_entities": header_entities,
                "metadata_overridden": metadata_overridden,
                "tags_override": tags_override,
                "entities_override": entities_override,
                "source_path": source_path,
                "source_format": "markdown",
                "created_at": meta.get("created_at", now),
                "updated_at": now,
                "created_by": meta.get("created_by", "system"),
                "updated_by": "reindex",
            }
            self._add_staging_chunks(
                staging_doc_id=staging_doc_id,
                target_doc_id=doc_id,
                title=title,
                chunks=new_chunks,
                embeddings=embeddings,
                metadata=staging_metadata,
            )
            await self._notify_progress(progress_callback, 65, "等待写入锁")
            async with self.write_lock:
                await self._notify_progress(progress_callback, 75, "替换文档索引")
                await self.kb.mark_doc_updating(doc_id)
                await self.kb.delete_document(doc_id)
                deleted_old = True
                try:
                    await self.kb.add_document_chunks(
                        doc_id=doc_id, title=title,
                        chunks=new_chunks, embeddings=embeddings,
                        metadata=staging_metadata,
                    )
                    await self.kb.set_doc_content_hash(doc_id, new_c_hash)
                except Exception:
                    logger.exception(f"Document reindex write failed, restoring previous chunks: doc_id={doc_id}")
                    try:
                        if deleted_old:
                            await self._restore_document_chunks(doc_id, title, chunks, restore_hash)
                    except Exception:
                        logger.exception(f"Document restore failed after reindex write failure: doc_id={doc_id}")
                    raise
        except WriteLockError as exc:
            raise self._write_lock_conflict(
                f"写入锁被占用，文档「{title}」暂时无法重建索引",
                exc,
            )
        finally:
            self._delete_staging_chunks(staging_doc_id)

        logger.info(f"Document reindexed: doc_id={doc_id}, title={title}, old={len(chunks)}, new={len(new_chunks)}")
        await self.refresh_keyword_document_safely(doc_id, "reindex_document")
        await self._notify_progress(progress_callback, 100, "文档索引重建完成")
        return {"success": True, "doc_id": doc_id, "chunks_old": len(chunks), "chunks_new": len(new_chunks)}

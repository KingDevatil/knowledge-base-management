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
from ingestion import DocumentIngestionPipeline, IngestionResult
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
            )
        except WriteLockError:
            if pipeline.latest_task:
                self.ingestion_tasks[pipeline.latest_task.task_id] = pipeline.latest_task.to_dict()
                self.ingestion_task_payloads[pipeline.latest_task.task_id] = {
                    **payload,
                    "doc_id": pipeline.latest_task.doc_id,
                }
            logger.warning(f"Write lock busy when importing: title={title}, size={content_size_kb(content)}")
            raise HTTPException(
                status_code=423,
                detail=f"知识库写入锁被占用，文档「{title}」暂时无法导入，请稍后重试",
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
    ) -> dict:
        """添加新文档"""
        tags = tags or []
        result = await self._import_document(title, content, path, tags, created_by)
        await self.refresh_keyword_index_safely("add_document")
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
        await self.refresh_keyword_index_safely("retry_ingestion_task")
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
            if "tags" in item and isinstance(item["tags"], list):
                item["tags"] = ",".join(item["tags"])
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
        await self._snapshot_document_version(doc_id, "before_update", updated_by)
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
            [t.strip() for t in old_tags_raw.replace("，", ",").split(",") if t.strip()]
            if isinstance(old_tags_raw, str) and old_tags_raw
            else (old_tags_raw if isinstance(old_tags_raw, list) else [])
        )

        # 未传入新路径/标签时保留原值，避免 Agent 遗漏参数导致数据丢失
        new_path = DirectoryTree.validate_path(path) if path else old_path

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

        old_source_path = old_meta.get("source_path", "")
        old_source_content = ""
        try:
            if old_source_path:
                old_source_content = self.source_store.get_source_by_full_path(old_source_path) or ""
            else:
                old_source_content = self.source_store.get_source(doc_id, old_path) or ""
        except Exception:
            old_source_content = ""

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
        restore_hash = await self.kb.get_doc_content_hash(doc_id)
        source_path = ""
        staging_doc_id = f"{doc_id}__staging__{uuid.uuid4().hex}"
        staging_source_path = ""
        deleted_old = False

        try:
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
                    "source_path": staging_source_path,
                    "source_format": "markdown",
                    "created_at": old_meta.get("created_at", now),
                    "updated_at": now,
                    "created_by": old_meta.get("created_by", updated_by),
                    "updated_by": updated_by,
                },
            )

            async with self.write_lock:
                try:
                    if old_path and old_path != new_path:
                        self.source_store.move_source(doc_id, old_path, new_path)
                    source_path = self.source_store.save_source(doc_id, content, new_path)
                    await self.kb.mark_doc_updating(doc_id)
                    await self.kb.delete_document(doc_id)
                    deleted_old = True
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
        except WriteLockError:
            raise HTTPException(
                status_code=423,
                detail=f"写入锁被占用，文档「{title}」暂时无法更新，请稍后重试",
            )
        finally:
            self._delete_staging_chunks(staging_doc_id)
            if staging_source_path:
                self._cleanup_staging_source(staging_source_path)

        await self.refresh_keyword_index_safely("update_document")

        return {"success": True, "doc_id": doc_id, "message": "文档更新成功"}

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
                await self.refresh_keyword_index_safely("delete_document")
        except WriteLockError:
            raise HTTPException(
                status_code=423,
                detail=f"写入锁被占用，文档「{title}」暂时无法删除",
            )

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
            meta.get("tags", "").replace("，", ",").split(",")
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
        restore_hash = await self.kb.get_doc_content_hash(doc_id)
        staging_doc_id = f"{doc_id}__staging__{uuid.uuid4().hex}"
        deleted_old = False

        try:
            now = datetime.now(timezone.utc).isoformat()
            staging_metadata = {
                "path": path, "tags": tags, "source_path": source_path,
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
            async with self.write_lock:
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
        except WriteLockError:
            raise HTTPException(status_code=423, detail=f"写入锁被占用，文档「{title}」暂时无法重建索引")
        finally:
            self._delete_staging_chunks(staging_doc_id)

        logger.info(f"Document reindexed: doc_id={doc_id}, title={title}, old={len(chunks)}, new={len(new_chunks)}")
        await self.refresh_keyword_index_safely("reindex_document")
        return {"success": True, "doc_id": doc_id, "chunks_old": len(chunks), "chunks_new": len(new_chunks)}

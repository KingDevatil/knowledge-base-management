"""Node-based document ingestion pipeline with task logs."""

from __future__ import annotations

import uuid
import inspect
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from fastapi import HTTPException

from chunker import chunk_markdown
from directory_tree import DirectoryTree
from embedding import EmbeddingError
from helpers import content_hash, content_size_kb


@dataclass
class IngestionNodeEvent:
    node: str
    status: str
    started_at: str
    duration_ms: float = 0.0
    input_summary: dict[str, Any] = field(default_factory=dict)
    output_summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class IngestionTaskRecord:
    task_id: str
    doc_id: str
    title: str
    status: str = "pending"
    current_node: str = ""
    created_by: str = "system"
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str = ""
    error: str = ""
    nodes: list[IngestionNodeEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IngestionResult:
    doc_id: str
    task: IngestionTaskRecord
    chunks: int
    source_path: str


class DocumentIngestionPipeline:
    def __init__(
        self,
        kb: Any,
        source_store: Any,
        embedder: Any,
        write_lock: Any,
        chunk_size: int,
        chunk_overlap: int,
    ):
        self.kb = kb
        self.source_store = source_store
        self.embedder = embedder
        self.write_lock = write_lock
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.latest_task: IngestionTaskRecord | None = None

    async def import_document(
        self,
        title: str,
        content: str,
        path: str,
        tags: list[str],
        created_by: str = "system",
        doc_id: str | None = None,
    ) -> IngestionResult:
        doc_id = doc_id or str(uuid.uuid4())
        task = IngestionTaskRecord(
            task_id=str(uuid.uuid4()),
            doc_id=doc_id,
            title=title,
            created_by=created_by,
            status="running",
        )
        self.latest_task = task

        context: dict[str, Any] = {
            "doc_id": doc_id,
            "title": title,
            "content": content,
            "path": path,
            "tags": tags,
            "created_by": created_by,
        }

        try:
            await self._run_node(task, "parse_markdown", self._parse_markdown, context)
            await self._run_node(task, "normalize_content", self._normalize_content, context)
            await self._run_node(task, "chunk", self._chunk, context)
            await self._run_node(task, "embedding", self._embedding, context)
            async with self.write_lock:
                context["now"] = datetime.now(timezone.utc).isoformat()
                await self._run_node(task, "persist_source", self._persist_source, context)
                await self._run_node(task, "persist_vector", self._persist_vector, context)
                await self._run_node(task, "commit_index", self._commit_index, context)
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.finished_at = datetime.now(timezone.utc).isoformat()
            self._cleanup_failed_import(context)
            raise

        task.status = "succeeded"
        task.current_node = ""
        task.finished_at = datetime.now(timezone.utc).isoformat()
        return IngestionResult(
            doc_id=doc_id,
            task=task,
            chunks=len(context["chunks"]),
            source_path=context["source_path"],
        )

    async def _run_node(self, task: IngestionTaskRecord, node: str, handler, context: dict[str, Any]) -> None:
        task.current_node = node
        event = IngestionNodeEvent(
            node=node,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            input_summary=self._summarize_context(context),
        )
        task.nodes.append(event)
        started = monotonic()
        try:
            result = handler(context)
            if inspect.isawaitable(result):
                await result
            event.status = "succeeded"
            event.output_summary = self._summarize_context(context)
        except Exception as exc:
            event.status = "failed"
            event.error = str(exc)
            event.output_summary = self._summarize_context(context)
            raise
        finally:
            event.duration_ms = round((monotonic() - started) * 1000, 3)

    def _parse_markdown(self, context: dict[str, Any]) -> None:
        title = context.get("title", "")
        content = context.get("content", "")
        doc_id = context.get("doc_id", "")
        if not title or not title.strip():
            raise HTTPException(status_code=400, detail="文档标题不能为空，请提供有效的标题")
        if not content or not content.strip():
            raise HTTPException(
                status_code=400,
                detail=f"文档「{title}」内容不能为空，请提供有效的 Markdown 内容",
            )
        context["size_label"] = content_size_kb(content)
        context["doc_id"] = doc_id

    def _normalize_content(self, context: dict[str, Any]) -> None:
        context["content"] = context["content"].replace("\r\n", "\n").replace("\r", "\n")
        context["path"] = DirectoryTree.validate_path(context.get("path", ""))
        context["tags"] = context.get("tags") or []

    def _chunk(self, context: dict[str, Any]) -> None:
        chunks = chunk_markdown(context["content"], self.chunk_size, self.chunk_overlap)
        if not chunks:
            raise HTTPException(
                status_code=400,
                detail=f"文档「{context['title']}」(doc_id={context['doc_id']}) 内容无法切片。"
                f"内容大小: {context['size_label']}，可能全部为空白字符，请检查内容",
            )
        context["chunks"] = chunks

    async def _embedding(self, context: dict[str, Any]) -> None:
        try:
            embeddings = await self.embedder.embed(context["chunks"])
        except EmbeddingError as e:
            raise HTTPException(
                status_code=503,
                detail=f"文档「{context['title']}」(doc_id={context['doc_id']}) Embedding 生成失败。"
                f"切片数: {len(context['chunks'])}，内容大小: {context['size_label']}。\n{e}",
            )
        if not embeddings or len(embeddings) != len(context["chunks"]):
            raise HTTPException(
                status_code=503,
                detail=f"文档「{context['title']}」(doc_id={context['doc_id']}) Embedding 生成失败。"
                f"期望 {len(context['chunks'])} 个向量，实际收到 {len(embeddings) if embeddings else 0} 个。",
            )
        context["embeddings"] = embeddings

    def _persist_source(self, context: dict[str, Any]) -> None:
        context["source_path"] = self.source_store.save_source(
            context["doc_id"],
            context["content"],
            context["path"],
        )

    def _persist_vector(self, context: dict[str, Any]) -> None:
        context["metadata"] = {
            "path": context["path"],
            "tags": context["tags"],
            "source_path": context["source_path"],
            "source_format": "markdown",
            "created_at": context["now"],
            "updated_at": context["now"],
            "created_by": context["created_by"],
            "updated_by": context["created_by"],
        }

    async def _commit_index(self, context: dict[str, Any]) -> None:
        await self.kb.add_document_chunks(
            doc_id=context["doc_id"],
            title=context["title"],
            chunks=context["chunks"],
            embeddings=context["embeddings"],
            metadata=context["metadata"],
        )
        await self.kb.set_doc_content_hash(context["doc_id"], content_hash(context["content"]))

    def _cleanup_failed_import(self, context: dict[str, Any]) -> None:
        source_path = context.get("source_path")
        if not source_path:
            return
        try:
            self.source_store.delete_source_by_path(source_path)
        except Exception:
            pass

    def _summarize_context(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "doc_id": context.get("doc_id", ""),
            "title": context.get("title", ""),
            "path": context.get("path", ""),
            "content_chars": len(context.get("content") or ""),
            "chunk_count": len(context.get("chunks") or []),
            "embedding_count": len(context.get("embeddings") or []),
            "source_path": context.get("source_path", ""),
        }

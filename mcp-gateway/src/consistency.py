"""Consistency diagnostics for knowledge-base storage layers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


@dataclass
class ConsistencyIssue:
    code: str
    severity: str
    message: str
    doc_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class KnowledgeBaseConsistencyChecker:
    """Checks drift between document index, Chroma chunks, and source files."""

    def __init__(self, kb: Any, source_store: Any):
        self.kb = kb
        self.source_store = source_store

    async def check(self) -> dict[str, Any]:
        issues: list[ConsistencyIssue] = []
        indexed_docs = await self._indexed_docs()
        indexed_by_id = {doc["doc_id"]: doc for doc in indexed_docs if doc.get("doc_id")}
        chroma_doc_ids = self._collect_chroma_doc_ids(issues)

        for doc_id, doc in indexed_by_id.items():
            chunks = await self.kb.get_document_chunks(doc_id)
            expected_count = self._safe_int(doc.get("chunk_count"))

            if not chunks:
                issues.append(
                    ConsistencyIssue(
                        code="missing_chunks",
                        severity="error",
                        doc_id=doc_id,
                        message="Document exists in index but has no Chroma chunks.",
                        details={"title": doc.get("title"), "path": doc.get("path", "")},
                    )
                )
            elif expected_count is not None and expected_count != len(chunks):
                issues.append(
                    ConsistencyIssue(
                        code="chunk_count_mismatch",
                        severity="warning",
                        doc_id=doc_id,
                        message="Indexed chunk_count differs from actual Chroma chunk count.",
                        details={"indexed": expected_count, "actual": len(chunks)},
                    )
                )

            if not await self._source_exists(doc, chunks):
                issues.append(
                    ConsistencyIssue(
                        code="missing_source",
                        severity="error",
                        doc_id=doc_id,
                        message="Document source file is missing or unreadable.",
                        details={"title": doc.get("title"), "path": doc.get("path", "")},
                    )
                )

        for doc_id in sorted(chroma_doc_ids - set(indexed_by_id)):
            issues.append(
                ConsistencyIssue(
                    code="orphan_chroma_document",
                    severity="warning",
                    doc_id=doc_id,
                    message="Chroma contains chunks for a document missing from the index.",
                )
            )

        severities = self._severity_counts(issues)
        return {
            "success": severities.get("error", 0) == 0,
            "issue_count": len(issues),
            "issues": [issue.to_dict() for issue in issues],
            "stats": {
                "indexed_documents": len(indexed_by_id),
                "chroma_documents": len(chroma_doc_ids),
                "errors": severities.get("error", 0),
                "warnings": severities.get("warning", 0),
            },
        }

    async def _indexed_docs(self) -> list[dict[str, Any]]:
        docs = await self.kb._doc_index_all()
        return [doc for doc in docs if isinstance(doc, dict)]

    def _collect_chroma_doc_ids(self, issues: list[ConsistencyIssue]) -> set[str]:
        results = self.kb.collection.get(include=["metadatas"])
        metadatas = results.get("metadatas") or []
        doc_ids: set[str] = set()

        for index, metadata in enumerate(metadatas):
            if not isinstance(metadata, dict):
                issues.append(
                    ConsistencyIssue(
                        code="invalid_chroma_metadata",
                        severity="warning",
                        message="Chroma chunk metadata is not an object.",
                        details={"chunk_index": index},
                    )
                )
                continue

            doc_id = metadata.get("doc_id")
            if not doc_id:
                issues.append(
                    ConsistencyIssue(
                        code="missing_chroma_doc_id",
                        severity="warning",
                        message="Chroma chunk metadata is missing doc_id.",
                        details={"chunk_index": index},
                    )
                )
                continue
            doc_ids.add(str(doc_id))

        return doc_ids

    async def _source_exists(self, doc: dict[str, Any], chunks: Iterable[dict[str, Any]]) -> bool:
        doc_id = str(doc.get("doc_id") or "")
        path = str(doc.get("path") or "")
        source_path = self._source_path_from_chunks(chunks)

        if source_path and hasattr(self.source_store, "get_source_by_full_path"):
            try:
                self.source_store.get_source_by_full_path(source_path)
                return True
            except Exception:
                return False

        if hasattr(self.source_store, "source_exists"):
            try:
                return bool(self.source_store.source_exists(doc_id, path))
            except Exception:
                return False

        if hasattr(self.source_store, "get_source"):
            try:
                self.source_store.get_source(doc_id, path)
                return True
            except Exception:
                return False

        return False

    def _source_path_from_chunks(self, chunks: Iterable[dict[str, Any]]) -> str:
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            metadata = chunk.get("metadata") or {}
            if isinstance(metadata, dict) and metadata.get("source_path"):
                return str(metadata["source_path"])
        return ""

    def _safe_int(self, value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _severity_counts(self, issues: Iterable[ConsistencyIssue]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for issue in issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        return counts

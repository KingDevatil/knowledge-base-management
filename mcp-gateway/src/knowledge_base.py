from typing import List, Optional
import chromadb
from chromadb.config import Settings as ChromaSettings

from models import SearchResult, DocumentInfo


class KnowledgeBase:
    """Chroma 知识库封装"""

    def __init__(self, chroma_client, collection_name: str):
        self.client = chroma_client
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"}
        )

    async def add_document_chunks(
        self,
        doc_id: str,
        title: str,
        chunks: List[str],
        embeddings: List[List[float]],
        metadata: dict
    ) -> None:
        """将切片批量写入 Chroma"""
        if not chunks:
            return

        ids = [f"{doc_id}#chunk-{i}" for i in range(len(chunks))]
        metadatas = [{
            **metadata,
            "doc_id": doc_id,
            "title": title,
            "chunk_index": i,
            "total_chunks": len(chunks),
        } for i in range(len(chunks))]

        # 确保 tags 是字符串（Chroma 不支持 list）
        for m in metadatas:
            if "tags" in m and isinstance(m["tags"], list):
                m["tags"] = ",".join(m["tags"])

        self.collection.add(
            ids=ids,
            documents=chunks,
            embeddings=embeddings,
            metadatas=metadatas
        )

    async def delete_document(self, doc_id: str) -> int:
        """删除某文档的所有切片，返回删除数量"""
        results = self.collection.get(where={"doc_id": doc_id})
        if results["ids"]:
            self.collection.delete(ids=results["ids"])
        return len(results["ids"])

    async def search(
        self,
        query_embedding: List[float],
        top_k: int = 5,
        filter_tags: Optional[List[str]] = None,
        filter_path: str = ""
    ) -> List[SearchResult]:
        """向量检索，支持按目录路径和标签筛选"""
        where_clause = {}
        if filter_tags:
            where_clause["tags"] = {"$contains": ",".join(filter_tags)}
        if filter_path:
            # 精确匹配目录路径
            where_clause["path"] = {"$eq": filter_path}

        where = where_clause if where_clause else None

        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"]
        )

        search_results = []
        if results["ids"] and len(results["ids"]) > 0:
            ids = results["ids"][0]
            documents = results["documents"][0] if results["documents"] else []
            metadatas = results["metadatas"][0] if results["metadatas"] else []
            distances = results["distances"][0] if results["distances"] else []

            for i in range(len(ids)):
                meta = metadatas[i] if i < len(metadatas) else {}
                search_results.append(SearchResult(
                    content=documents[i] if i < len(documents) else "",
                    title=meta.get("title", ""),
                    path=meta.get("path", ""),
                    source_path=meta.get("source_path", ""),
                    doc_id=meta.get("doc_id", ""),
                    chunk_index=meta.get("chunk_index", 0),
                    total_chunks=meta.get("total_chunks", 0),
                    score=1.0 - (distances[i] if i < len(distances) else 0.0),
                ))

        return search_results

    async def list_documents(
        self,
        tags: Optional[List[str]] = None,
        path: str = "",
        limit: int = 20,
        offset: int = 0
    ) -> List[DocumentInfo]:
        """按条件列出文档，支持目录路径筛选"""
        where_clause = {}
        if tags:
            where_clause["tags"] = {"$contains": ",".join(tags)}
        if path:
            where_clause["path"] = {"$eq": path}

        where = where_clause if where_clause else None

        results = self.collection.get(
            where=where,
            include=["metadatas"]
        )

        # 按 doc_id 去重聚合
        docs = {}
        if results["metadatas"]:
            for meta in results["metadatas"]:
                doc_id = meta.get("doc_id", "")
                if not doc_id:
                    continue
                if doc_id not in docs:
                    tags_val = meta.get("tags", "")
                    if isinstance(tags_val, str):
                        tags_list = [t.strip() for t in tags_val.split(",") if t.strip()]
                    else:
                        tags_list = tags_val if isinstance(tags_val, list) else []

                    docs[doc_id] = {
                        "doc_id": doc_id,
                        "title": meta.get("title", ""),
                        "path": meta.get("path", ""),
                        "tags": tags_list,
                        "chunk_count": 0,
                        "created_at": meta.get("created_at", ""),
                        "updated_at": meta.get("updated_at", ""),
                    }
                docs[doc_id]["chunk_count"] += 1

        doc_list = list(docs.values())
        doc_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return [DocumentInfo(**d) for d in doc_list[offset:offset + limit]]

    async def get_document_chunks(self, doc_id: str) -> List[dict]:
        """获取某文档的所有切片"""
        results = self.collection.get(
            where={"doc_id": doc_id},
            include=["documents", "metadatas"]
        )

        chunks = []
        if results["ids"]:
            for i in range(len(results["ids"])):
                chunks.append({
                    "id": results["ids"][i],
                    "content": results["documents"][i] if results["documents"] else "",
                    "metadata": results["metadatas"][i] if results["metadatas"] else {},
                })
            chunks.sort(key=lambda x: x["metadata"].get("chunk_index", 0))
        return chunks

    async def count_documents(self) -> int:
        """获取文档总数（去重后的 doc_id 数量）"""
        results = self.collection.get(include=["metadatas"])
        if not results["metadatas"]:
            return 0
        return len(set(m.get("doc_id", "") for m in results["metadatas"] if m.get("doc_id")))

import json
from typing import List, Optional
import chromadb

from models import SearchResult, DocumentInfo

# Redis key for document index cache
DOC_INDEX_KEY = "kb:doc_index"


class KnowledgeBase:
    """Chroma 知识库封装"""

    def __init__(self, chroma_client, collection_name: str):
        self.client = chroma_client
        # HNSW 索引参数：hnsw:M 增大到 32 提升召回率（默认 16）
        # hnsw:ef_construction 在 Chroma 1.5+ 才支持，当前版本仅使用 hnsw:M
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={
                "hnsw:space": "cosine",
                "hnsw:M": 32,
            }
        )
        self._redis = None  # set externally via set_redis()

    def set_redis(self, redis_client) -> None:
        """注入 Redis 客户端，用于文档索引缓存"""
        self._redis = redis_client

    # ---- Redis doc index helpers ----

    async def _doc_index_get(self, doc_id: str) -> Optional[dict]:
        if not self._redis:
            return None
        raw = await self._redis.hget(DOC_INDEX_KEY, doc_id)
        return json.loads(raw) if raw else None

    async def _doc_index_set(self, doc_id: str, info: dict) -> None:
        if not self._redis:
            return
        await self._redis.hset(DOC_INDEX_KEY, doc_id, json.dumps(info, ensure_ascii=False))

    async def _doc_index_delete(self, doc_id: str) -> None:
        if not self._redis:
            return
        await self._redis.hdel(DOC_INDEX_KEY, doc_id)

    async def mark_doc_updating(self, doc_id: str) -> None:
        """标记文档为 '更新中' 状态，搜索/列表时暂时跳过"""
        info = await self._doc_index_get(doc_id)
        if info:
            info["__write_status"] = "updating"
            await self._doc_index_set(doc_id, info)

    async def set_doc_content_hash(self, doc_id: str, sha256_hash: str) -> None:
        """缓存文档内容的 SHA256 哈希到 Redis，用于变更检测"""
        info = await self._doc_index_get(doc_id) or {}
        info["content_hash"] = sha256_hash
        await self._doc_index_set(doc_id, info)

    async def get_doc_content_hash(self, doc_id: str) -> str | None:
        """从 Redis 读取文档内容哈希，不存在返回 None"""
        info = await self._doc_index_get(doc_id)
        return info.get("content_hash") if info else None

    async def _doc_index_all(self) -> List[dict]:
        """从 Redis 获取所有文档索引（跳过正在写入中的文档）"""
        if not self._redis:
            return []
        raw = await self._redis.hgetall(DOC_INDEX_KEY)
        docs = [json.loads(v) for v in raw.values()]
        # 过滤掉正在更新中的文档（锁内状态标记）
        return [d for d in docs if d.get("__write_status") != "updating"]

    async def _doc_index_rebuild(self) -> int:
        """从 Chroma 重建文档索引到 Redis（首调用时或数据不一致时使用）"""
        results = self.collection.get(include=["metadatas"])
        if not results["metadatas"]:
            return 0

        docs = {}
        for meta in results["metadatas"]:
            doc_id = meta.get("doc_id", "")
            if not doc_id:
                continue
            if doc_id not in docs:
                docs[doc_id] = {
                    "doc_id": doc_id,
                    "title": meta.get("title", ""),
                    "path": meta.get("path", ""),
                    "tags": meta.get("tags", ""),
                    "chunk_count": 0,
                    "created_at": meta.get("created_at", ""),
                    "updated_at": meta.get("updated_at", ""),
                }
            docs[doc_id]["chunk_count"] += 1
            # 取最新 updated_at
            u = meta.get("updated_at", "")
            if u > docs[doc_id]["updated_at"]:
                docs[doc_id]["updated_at"] = u

        if self._redis:
            pipe = self._redis.pipeline()
            for doc_id, info in docs.items():
                pipe.hset(DOC_INDEX_KEY, doc_id, json.dumps(info, ensure_ascii=False))
            await pipe.execute()

        return len(docs)

    # ---- Core methods ----

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

        for m in metadatas:
            if "tags" in m and isinstance(m["tags"], list):
                m["tags"] = ",".join(m["tags"])

        self.collection.add(
            ids=ids,
            documents=chunks,
            embeddings=embeddings,
            metadatas=metadatas
        )

        # 更新 Redis 文档索引
        await self._doc_index_set(doc_id, {
            "doc_id": doc_id,
            "title": title,
            "path": metadata.get("path", ""),
            "tags": metadata.get("tags", ""),
            "chunk_count": len(chunks),
            "created_at": metadata.get("created_at", ""),
            "updated_at": metadata.get("updated_at", ""),
        })

    async def delete_document(self, doc_id: str) -> int:
        """删除某文档的所有切片，返回删除数量"""
        results = self.collection.get(where={"doc_id": doc_id})
        if results["ids"]:
            self.collection.delete(ids=results["ids"])
        await self._doc_index_delete(doc_id)
        return len(results["ids"])

    async def search(
        self,
        query_embedding: List[float],
        top_k: int = 5,
        filter_tags: Optional[List[str]] = None,
        filter_path: str = ""
    ) -> List[SearchResult]:
        """向量检索"""
        where_clause = {}
        if filter_tags:
            where_clause["tags"] = {"$contains": ",".join(filter_tags)}
        if filter_path:
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
        """按条件列出文档 — 优先读 Redis 索引，支持分页"""
        # 优先从 Redis 索引读取
        doc_list = await self._doc_index_all()
        if not doc_list:
            # 缓存缺失，从 Chroma 重建
            await self._doc_index_rebuild()
            doc_list = await self._doc_index_all()

        # 过滤
        if path:
            doc_list = [d for d in doc_list if d.get("path", "") == path]
        if tags:
            tag_set = set(tags)
            doc_list = [
                d for d in doc_list
                if tag_set & set(
                    d.get("tags", "").split(",") if isinstance(d.get("tags"), str) else []
                )
            ]

        # 排序
        doc_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        # 真正分页
        return [DocumentInfo(**d) for d in doc_list[offset:offset + limit]]

    async def list_documents_by_paths(
        self,
        paths: List[str],
        limit: int = 20,
        offset: int = 0
    ) -> List[DocumentInfo]:
        """列出多个目录（含子目录前缀匹配）下的文档"""
        doc_list = await self._doc_index_all()
        if not doc_list:
            await self._doc_index_rebuild()
            doc_list = await self._doc_index_all()

        if paths:
            doc_list = [
                d for d in doc_list
                if any(d.get("path", "") == p or d.get("path", "").startswith(p + "/")
                       for p in paths)
            ]

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
        """获取文档总数"""
        doc_list = await self._doc_index_all()
        if doc_list:
            return len(doc_list)
        # 回退：从 Chroma 统计
        results = self.collection.get(include=["metadatas"])
        if not results["metadatas"]:
            return 0
        return len(set(m.get("doc_id", "") for m in results["metadatas"] if m.get("doc_id")))

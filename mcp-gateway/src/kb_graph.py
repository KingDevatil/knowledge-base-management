"""知识图谱构建器：基于文档元数据构建关系图谱，使用 Graphify 管线导出可视化。"""
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx

try:
    from graphify.build import build_from_json
    from graphify.cluster import cluster
    from graphify.export import to_html, to_json
except ImportError:  # 允许基础检索在可选图谱依赖尚未安装时继续启动
    build_from_json = None
    cluster = None
    to_html = None
    to_json = None

from config import get_settings
from logger import get_logger

logger = get_logger()


class KnowledgeGraphBuilder:
    """从知识库文档元数据构建关系图谱。

    边类型：
      - co_tag (EXTRACTED): 共享至少一个标签的两篇文档
      - same_directory (EXTRACTED): 同一目录下的文档
      - semantically_similar (INFERRED, 可选): 向量余弦相似度超过阈值
    """

    def __init__(self, kb, embedder=None):
        self.kb = kb
        self.embedder = embedder
        self.settings = get_settings()

    @property
    def graph_dir(self) -> Path:
        data_dir = self.settings.KBDATA_DIR or "kbdata"
        return Path(data_dir) / "graph"

    @property
    def graph_html_path(self) -> Path:
        return self.graph_dir / "graph.html"

    @property
    def graph_json_path(self) -> Path:
        return self.graph_dir / "graph.json"

    @property
    def labels_path(self) -> Path:
        return self.graph_dir / ".graphify_labels.json"

    @property
    def retrieval_json_path(self) -> Path:
        return self.graph_dir / "retrieval_index.json"

    async def build(self, semantic_threshold: float = 0.0) -> dict:
        """全量构建：获取文档 → 构造节点/边 → 建图 → 聚类 → 导出。"""
        if not all((build_from_json, cluster, to_html, to_json)):
            raise RuntimeError(
                "知识图谱依赖未安装，请执行 pip install -r mcp-gateway/requirements.txt"
            )
        # 1. 获取所有文档
        logger.info("Fetching all documents for graph build...")
        docs_result = await self.kb.list_documents(limit=10000, offset=0)
        docs = docs_result[0] if isinstance(docs_result, tuple) else docs_result.get("documents", [])
        if not docs:
            logger.info("No documents found, skipping graph build.")
            return {"success": True, "message": "知识库为空，跳过图谱构建", "node_count": 0, "edge_count": 0, "community_count": 0}

        # 统一格式：Pydantic model → dict
        docs = [d.model_dump() if callable(getattr(d, "model_dump", None)) else d for d in docs]

        doc_count = len(docs)
        logger.info(f"Building graph from {doc_count} documents")

        # 2. 构造 extraction dict
        extraction = self._build_extraction(docs, semantic_threshold)

        # 3. 构建 NetworkX 图
        G = build_from_json(extraction, directed=False)
        node_count = G.number_of_nodes()
        edge_count = G.number_of_edges()
        logger.info(f"Graph built: {node_count} nodes, {edge_count} edges")

        # 4. 社区聚类
        if node_count == 0:
            return {"success": True, "message": "图谱为空", "node_count": 0, "edge_count": 0, "community_count": 0}

        communities = cluster(G, resolution=1.0)
        logger.info(f"Community detection: {len(communities)} communities")

        # 5. 社区标签
        community_labels = self._build_community_labels(communities, G)
        member_counts = {cid: len(members) for cid, members in communities.items()}

        # 6. 确保输出目录
        self.graph_dir.mkdir(parents=True, exist_ok=True)

        # 7. 导出 HTML
        to_html(
            G,
            communities,
            str(self.graph_html_path),
            community_labels=community_labels,
            member_counts=member_counts,
        )
        logger.info(f"HTML exported: {self.graph_html_path}")

        # 8. 导出 JSON
        to_json(G, communities, str(self.graph_json_path), force=True)
        logger.info(f"JSON exported: {self.graph_json_path}")

        # 保存稳定、轻量的检索索引，避免检索链路依赖可视化库的 JSON 结构。
        self.retrieval_json_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "nodes": extraction["nodes"],
                    "edges": extraction["edges"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info(f"Retrieval graph exported: {self.retrieval_json_path}")

        # 9. 保存社区标签
        self.labels_path.write_text(
            json.dumps(community_labels, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return {
            "success": True,
            "node_count": node_count,
            "edge_count": edge_count,
            "community_count": len(communities),
            "html_path": str(self.graph_html_path),
            "json_path": str(self.graph_json_path),
            "retrieval_index_path": str(self.retrieval_json_path),
            "message": f"图谱构建完成: {node_count} 节点, {edge_count} 边, {len(communities)} 社区",
        }

    def _build_extraction(self, docs: list, semantic_threshold: float) -> dict:
        """构造 extraction dict。"""
        nodes: list[dict] = []
        edges: list[dict] = []
        edge_keys: set[tuple[str, str, str]] = set()

        # 节点：每篇文档一个
        for doc in docs:
            doc_id = doc.get("doc_id") or doc.get("id", "")
            nodes.append({
                "id": doc_id,
                "label": doc.get("title", ""),
                "file_type": "document",
                "source_file": doc.get("path", "") or "/",
                "tags": ",".join(doc.get("tags", [])),
                "updated_at": doc.get("updated_at", ""),
            })

        def _add_edge(
            src: str,
            tgt: str,
            relation: str,
            confidence: str,
            weight: float,
            **attributes,
        ):
            if not src or not tgt or src == tgt:
                return
            left, right = sorted((src, tgt))
            key = (left, right, relation)
            if key in edge_keys:
                return
            edge_keys.add(key)
            edges.append({
                "source": left,
                "target": right,
                "relation": relation,
                "confidence": confidence,
                "weight": round(min(max(float(weight), 0.0), 1.0), 4),
                "source_file": "kb",
                **attributes,
            })

        # co_tag 边：共享标签
        tag_index: dict[str, list[str]] = {}
        for doc in docs:
            doc_id = doc.get("doc_id") or doc.get("id", "")
            for tag in doc.get("tags", []):
                t = tag.strip()
                if t:
                    tag_index.setdefault(t, []).append(doc_id)

        shared_tags: dict[tuple[str, str], list[str]] = {}
        for tag, ids in tag_index.items():
            if len(ids) < 2:
                continue
            for src, tgt in self._relation_pairs(ids):
                pair = tuple(sorted((src, tgt)))
                shared_tags.setdefault(pair, []).append(tag)
        for (src, tgt), tags in shared_tags.items():
            _add_edge(
                src,
                tgt,
                "co_tag",
                "EXTRACTED",
                min(0.75, 0.45 + 0.10 * (len(tags) - 1)),
                shared_tags=tags,
            )

        # same_directory 边：同一非根目录下的文档，补齐原设计中未落地的结构关系。
        directory_index: dict[str, list[str]] = {}
        for doc in docs:
            path = str(doc.get("path", "")).strip()
            doc_id = doc.get("doc_id") or doc.get("id", "")
            if path and path != "/" and doc_id:
                directory_index.setdefault(path, []).append(doc_id)
        for path, ids in directory_index.items():
            for src, tgt in self._relation_pairs(ids):
                _add_edge(
                    src, tgt, "same_directory", "EXTRACTED", 0.35,
                    directory=path,
                )

        # semantically_similar 边（可选）
        if semantic_threshold > 0.0:
            try:
                semantic_edges = self._compute_semantic_edges(docs, semantic_threshold)
                for src, tgt, similarity in semantic_edges:
                    _add_edge(
                        src, tgt, "semantically_similar", "INFERRED", similarity,
                        similarity=round(similarity, 4),
                    )
            except Exception as e:
                logger.warning(f"Semantic similarity computation failed: {e}")

        return {"nodes": nodes, "edges": edges}

    @staticmethod
    def _relation_pairs(ids: list[str], clique_limit: int = 50):
        """Use a full clique for small groups and a two-hop sparse star for large ones."""
        unique_ids = list(dict.fromkeys(doc_id for doc_id in ids if doc_id))
        if len(unique_ids) <= clique_limit:
            for i in range(len(unique_ids)):
                for j in range(i + 1, len(unique_ids)):
                    yield unique_ids[i], unique_ids[j]
            return
        hub = unique_ids[0]
        for doc_id in unique_ids[1:]:
            yield hub, doc_id

    def _compute_semantic_edges(self, docs: list, threshold: float) -> list[tuple[str, str, float]]:
        """从 Chroma 取 embedding 计算文档间余弦相似度。"""
        doc_ids = [d.get("doc_id") or d.get("id", "") for d in docs]
        chunk_ids = [f"{did}#chunk-0" for did in doc_ids]

        try:
            result = self.kb.collection.get(ids=chunk_ids, include=["embeddings"])
        except Exception as e:
            logger.warning(f"Failed to get embeddings from Chroma: {e}")
            return []

        raw_embeddings = result.get("embeddings") if result else None
        if raw_embeddings is None or len(raw_embeddings) == 0:
            return []

        # Chroma 返回的 embeddings 可能是 numpy.ndarray，转为 Python list
        try:
            import numpy
            if isinstance(raw_embeddings, numpy.ndarray):
                embeddings_list = raw_embeddings.tolist()
            else:
                embeddings_list = list(raw_embeddings)
        except ImportError:
            embeddings_list = list(raw_embeddings)

        doc_emb: dict[str, list[float]] = {}
        ids = result.get("ids", [])
        for i, cid in enumerate(ids):
            did = cid.split("#")[0]
            if i < len(embeddings_list) and embeddings_list[i]:
                doc_emb[did] = embeddings_list[i]

        ids = list(doc_emb.keys())
        if len(ids) < 2:
            return []

        edges: list[tuple[str, str, float]] = []
        logger.info(f"Computing semantic similarity for {len(ids)} docs (threshold={threshold})")

        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                sim = self._cosine_similarity(doc_emb[ids[i]], doc_emb[ids[j]])
                if sim >= threshold:
                    edges.append((ids[i], ids[j], sim))

        logger.info(f"Semantic edges: {len(edges)} above threshold {threshold}")
        return edges

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    @staticmethod
    def _build_community_labels(communities: dict[int, list[str]], G: nx.Graph) -> dict[int, str]:
        """根据社区内最常见标签或目录生成可读标签。"""
        labels: dict[int, str] = {}
        for cid, members in communities.items():
            tag_count: dict[str, int] = {}
            path_count: dict[str, int] = {}
            for nid in members:
                node = G.nodes[nid]
                tags_str = node.get("tags", "")
                for t in tags_str.split(","):
                    t = t.strip()
                    if t:
                        tag_count[t] = tag_count.get(t, 0) + 1
                src = node.get("source_file", "")
                if src and src != "/":
                    path_count[src] = path_count.get(src, 0) + 1

            if tag_count:
                best = max(tag_count, key=tag_count.get)
                labels[cid] = f"#{best}"
            elif path_count:
                best = max(path_count, key=path_count.get)
                labels[cid] = best
            else:
                labels[cid] = f"Community {cid}"
        return labels

from typing import List, Optional
from source_store import SourceStore


class DirectoryTree:
    """目录树聚合与维护"""

    @staticmethod
    def build_from_metadata(metadatas: List[dict]) -> dict:
        """从文档元数据构建目录树"""
        tree = {"name": "root", "path": "", "children": {}}

        for meta in metadatas:
            path = meta.get("path", "")
            if not path:
                continue

            parts = [p for p in path.split("/") if p]
            current = tree
            current_path = ""

            for part in parts:
                current_path = f"{current_path}/{part}".strip("/")
                if part not in current["children"]:
                    current["children"][part] = {
                        "name": part,
                        "path": current_path,
                        "children": {}
                    }
                current = current["children"][part]

        def dict_to_list(node: dict) -> dict:
            children_list = sorted(
                [dict_to_list(child) for child in node["children"].values()],
                key=lambda x: x["name"]
            )
            return {
                "name": node["name"],
                "path": node["path"],
                "children": children_list
            }

        return dict_to_list(tree)

    @staticmethod
    def build_from_minio(source_store: SourceStore) -> dict:
        """从 MinIO 对象列表构建目录树"""
        docs = source_store.list_all_documents()
        seen = {}
        for doc in docs:
            seen[doc["doc_id"]] = doc

        metadatas = [{"path": doc["path"]} for doc in seen.values()]
        return DirectoryTree.build_from_metadata(metadatas)

    @staticmethod
    def validate_path(path: str) -> str:
        """验证并规范化路径"""
        if not path:
            return ""
        path = path.strip("/").replace("\\", "/")
        parts = [p for p in path.split("/") if p and p != ".."]
        return "/".join(parts)

    @staticmethod
    def get_breadcrumbs(path: str) -> List[dict]:
        """获取面包屑导航列表"""
        if not path:
            return []
        parts = [p for p in path.split("/") if p]
        breadcrumbs = []
        accum = ""
        for part in parts:
            accum = f"{accum}/{part}".strip("/")
            breadcrumbs.append({"name": part, "path": accum})
        return breadcrumbs

    @staticmethod
    def get_child_paths(path: str, all_paths: List[str]) -> List[str]:
        """获取某路径下的直接子目录"""
        path = DirectoryTree.validate_path(path)
        children = set()
        for p in all_paths:
            p = DirectoryTree.validate_path(p)
            if not p:
                continue
            if path and not p.startswith(path + "/"):
                continue
            if not path and "/" in p:
                children.add(p.split("/")[0])
                continue
            if path:
                remaining = p[len(path) + 1:]
                if "/" in remaining:
                    children.add(remaining.split("/")[0])
                else:
                    children.add(remaining)
        return sorted([c for c in children if c])

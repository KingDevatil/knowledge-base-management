"""
目录持久化存储
管理用户创建的目录结构，存于 JSON 文件。
即使目录为空（无文档），也会在目录树中显示。
"""
import json
import os
from typing import List

from config import get_settings


def _get_store_path() -> str:
    """获取目录存储文件路径"""
    settings = get_settings()
    base = os.path.dirname(settings.ADMIN_ACCOUNTS_FILE)  # 与 config/ 同目录
    return os.path.join(base, "directories.json")


def _load_dirs() -> List[str]:
    """加载所有已创建的目录路径"""
    path = _get_store_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def _save_dirs(dirs: List[str]) -> bool:
    """保存目录列表"""
    path = _get_store_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 去重 + 排序
        unique = sorted(set(d for d in dirs if d))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(unique, f, ensure_ascii=False, indent=2)
        return True
    except IOError:
        return False


def create_directory(path: str) -> bool:
    """创建目录（含父目录递归）"""
    dirs = _load_dirs()
    # 递归添加所有父级路径
    parts = path.strip("/").split("/")
    for i in range(len(parts)):
        parent = "/".join(parts[:i + 1])
        if parent not in dirs:
            dirs.append(parent)
    return _save_dirs(dirs)


def get_user_directories() -> List[str]:
    """获取用户创建的所有目录路径"""
    return _load_dirs()


def merge_into_tree(tree: dict) -> dict:
    """将用户创建的目录合并到目录树中"""
    user_dirs = get_user_directories()
    if not user_dirs:
        return tree

    # 从已有树中提取已存在的路径
    existing = set()

    def collect(node, prefix):
        if node.get("path"):
            existing.add(node["path"])
        for child in node.get("children", []):
            collect(child, prefix)

    if "children" in tree:
        for child in tree["children"]:
            collect(child, "")

    # 添加不存在的用户目录
    for udir in sorted(user_dirs):
        if udir not in existing:
            parts = udir.split("/")
            current = tree
            current_path = ""
            for part in parts:
                current_path = f"{current_path}/{part}".strip("/")
                found = None
                for child in current.get("children", []):
                    if child["name"] == part:
                        found = child
                        break
                if not found:
                    new_node = {"name": part, "path": current_path, "children": []}
                    if "children" not in current:
                        current["children"] = []
                    current["children"].append(new_node)
                    current["children"].sort(key=lambda x: x["name"])
                    found = new_node
                current = found

    return tree

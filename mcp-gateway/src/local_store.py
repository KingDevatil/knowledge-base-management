"""
本地文件存储（开发环境替代 MinIO）

在 kbdata/sources/ 目录下直接存储原始文件，
与 SourceStore 接口兼容，无需 MinIO 服务。
写操作用线程锁 + 临时文件 + 原子重命名保证安全。
"""
import os
import tempfile
import threading
from typing import List, Optional


class LocalFileStore:
    """本地文件系统存储（替代 MinIO 用于开发环境）"""

    def __init__(self, base_dir: str, bucket: str = "kb-sources"):
        self.base_dir = os.path.abspath(base_dir)
        self.bucket = bucket
        self._lock = threading.Lock()
        os.makedirs(os.path.join(self.base_dir, self.bucket, "documents"), exist_ok=True)

    def _build_path(self, doc_id: str, path: str = "") -> str:
        """构建本地存储路径"""
        if path:
            path = path.strip("/").replace("\\", "/")
            return f"documents/{path}/{doc_id}/source.md"
        return f"documents/{doc_id}/source.md"

    def _atomic_write(self, full_path: str, content: str) -> None:
        """原子写入：临时文件 + 重命名（newline='' 禁止平台换行翻译）"""
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        dirname = os.path.dirname(full_path)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=dirname, delete=False, suffix=".tmp") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        os.replace(tmp_path, full_path)

    def save_source(self, doc_id: str, content: str, path: str = "") -> str:
        """保存原始 Markdown"""
        object_path = self._build_path(doc_id, path)
        full_path = os.path.join(self.base_dir, self.bucket, object_path)
        with self._lock:
            self._atomic_write(full_path, content)
        return object_path

    def get_source(self, doc_id: str, path: str = "") -> str:
        """读取原始 Markdown（newline='' 禁止平台换行翻译）"""
        object_path = self._build_path(doc_id, path)
        full_path = os.path.join(self.base_dir, self.bucket, object_path)
        with open(full_path, "r", encoding="utf-8", newline="") as f:
            return f.read()

    def get_source_by_full_path(self, source_path: str) -> str:
        """通过完整路径读取"""
        full_path = os.path.join(self.base_dir, self.bucket, source_path)
        with open(full_path, "r", encoding="utf-8", newline="") as f:
            return f.read()

    def delete_source(self, doc_id: str, path: str = "") -> None:
        """删除源文件"""
        object_path = self._build_path(doc_id, path)
        full_path = os.path.join(self.base_dir, self.bucket, object_path)
        with self._lock:
            if os.path.exists(full_path):
                os.remove(full_path)

    def delete_source_by_path(self, source_path: str) -> None:
        """通过路径删除"""
        full_path = os.path.join(self.base_dir, self.bucket, source_path)
        with self._lock:
            if os.path.exists(full_path):
                os.remove(full_path)

    def move_source(self, doc_id: str, old_path: str, new_path: str) -> str:
        """移动文档目录"""
        old_object = self._build_path(doc_id, old_path)
        new_object = self._build_path(doc_id, new_path)
        old_full = os.path.join(self.base_dir, self.bucket, old_object)
        new_full = os.path.join(self.base_dir, self.bucket, new_object)
        with self._lock:
            os.makedirs(os.path.dirname(new_full), exist_ok=True)
            if os.path.exists(old_full):
                os.replace(old_full, new_full)
        return new_object

    def list_all_documents(self) -> List[dict]:
        """列出所有文档"""
        docs_dir = os.path.join(self.base_dir, self.bucket, "documents")
        result = []
        if not os.path.exists(docs_dir):
            return result
        for root, dirs, files in os.walk(docs_dir):
            for f in files:
                if f == "source.md":
                    relative = os.path.relpath(root, docs_dir).replace("\\", "/")
                    parts = relative.split("/")
                    if len(parts) >= 1:
                        doc_id = parts[-1]
                        doc_path = "/".join(parts[:-1]) if len(parts) > 1 else ""
                        source_path = os.path.join("documents", relative, "source.md").replace("\\", "/")
                        result.append({
                            "doc_id": doc_id,
                            "path": doc_path,
                            "source_path": source_path,
                        })
        return result

    def source_exists(self, doc_id: str, path: str = "") -> bool:
        """检查源文件是否存在"""
        object_path = self._build_path(doc_id, path)
        full_path = os.path.join(self.base_dir, self.bucket, object_path)
        return os.path.exists(full_path)

    def bucket_exists(self, bucket: str) -> bool:
        """检查桶是否存在（兼容 MinIO 接口）"""
        path = os.path.join(self.base_dir, bucket)
        return os.path.exists(path)

    def make_bucket(self, bucket: str) -> None:
        """创建桶"""
        path = os.path.join(self.base_dir, bucket)
        os.makedirs(path, exist_ok=True)

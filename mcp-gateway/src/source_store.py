import io
from typing import List, Optional

from minio import Minio
from minio.commonconfig import CopySource
from minio.error import S3Error

# Default timeout for MinIO operations (seconds)
MINIO_CONNECT_TIMEOUT = 5
MINIO_READ_TIMEOUT = 30


class SourceStore:
    """MinIO 源文件存储管理（支持多级目录树）"""

    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, secure: bool = False):
        import urllib3
        http_client = urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=MINIO_CONNECT_TIMEOUT, read=MINIO_READ_TIMEOUT),
            maxsize=10,
            retries=False,
        )
        self.client = Minio(endpoint, access_key, secret_key, secure=secure, http_client=http_client)
        self.bucket = bucket
        # 确保 bucket 存在
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    def _build_path(self, doc_id: str, path: str = "") -> str:
        """构建 MinIO 存储路径：documents/{path}/{doc_id}/source.md"""
        if path:
            path = path.strip("/").replace("\\", "/")
            return f"documents/{path}/{doc_id}/source.md"
        return f"documents/{doc_id}/source.md"

    def save_source(self, doc_id: str, content: str, path: str = "") -> str:
        """保存原始 Markdown，返回存储路径"""
        object_path = self._build_path(doc_id, path)
        data = content.encode("utf-8")
        self.client.put_object(
            self.bucket, object_path,
            data=io.BytesIO(data),
            length=len(data),
            content_type="text/markdown; charset=utf-8"
        )
        return object_path

    def get_source(self, doc_id: str, path: str = "") -> str:
        """读取原始 Markdown 内容"""
        object_path = self._build_path(doc_id, path)
        obj = self.client.get_object(self.bucket, object_path)
        return obj.read().decode("utf-8")

    def get_source_by_full_path(self, source_path: str) -> str:
        """通过完整 source_path 读取内容"""
        obj = self.client.get_object(self.bucket, source_path)
        return obj.read().decode("utf-8")

    def delete_source(self, doc_id: str, path: str = "") -> None:
        """删除源文件"""
        object_path = self._build_path(doc_id, path)
        try:
            self.client.remove_object(self.bucket, object_path)
        except S3Error as e:
            if e.code != "NoSuchKey":
                raise

    def delete_source_by_path(self, source_path: str) -> None:
        """通过完整路径删除源文件"""
        try:
            self.client.remove_object(self.bucket, source_path)
        except S3Error as e:
            if e.code != "NoSuchKey":
                raise

    def move_source(self, doc_id: str, old_path: str, new_path: str) -> str:
        """移动文档到新的目录路径"""
        old_object_path = self._build_path(doc_id, old_path)
        new_object_path = self._build_path(doc_id, new_path)

        # MinIO 没有原生 move，用 copy + delete
        self.client.copy_object(
            self.bucket, new_object_path,
            CopySource(self.bucket, old_object_path)
        )
        self.client.remove_object(self.bucket, old_object_path)
        return new_object_path

    def list_all_documents(self) -> List[dict]:
        """列出所有文档对象，用于构建目录树"""
        objects = self.client.list_objects(self.bucket, prefix="documents/", recursive=True)
        docs = []
        for obj in objects:
            if obj.object_name.endswith("/source.md"):
                # 解析 path 和 doc_id: documents/{path}/{doc_id}/source.md
                relative = obj.object_name[len("documents/"):]
                parts = relative.split("/")
                if len(parts) >= 2:
                    doc_id = parts[-2]
                    path = "/".join(parts[:-2]) if len(parts) > 2 else ""
                    docs.append({
                        "doc_id": doc_id,
                        "path": path,
                        "source_path": obj.object_name,
                        "size": obj.size,
                        "last_modified": obj.last_modified,
                    })
        return docs

    def source_exists(self, doc_id: str, path: str = "") -> bool:
        """检查源文件是否存在"""
        object_path = self._build_path(doc_id, path)
        try:
            self.client.stat_object(self.bucket, object_path)
            return True
        except S3Error:
            return False

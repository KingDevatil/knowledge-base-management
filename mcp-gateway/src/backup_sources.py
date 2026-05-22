#!/usr/bin/env python3
"""
知识库源文件备份工具

从 MinIO 拉取所有原始 Markdown 文件，按目录结构保存到本地。
用法:
    python backup_sources.py
    python backup_sources.py --output D:/backups/kb-20260522
"""

import argparse
import os
import sys
from datetime import datetime

from minio import Minio


def get_settings():
    """从环境变量读取 MinIO 配置（与 start-dev.ps1 一致）"""
    return {
        "endpoint": os.environ.get("MINIO_ENDPOINT", "localhost:9000"),
        "access_key": os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
        "secret_key": os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
        "bucket": os.environ.get("MINIO_BUCKET", "kb-sources"),
        "secure": os.environ.get("MINIO_SECURE", "false").lower() == "true",
    }


def backup_sources(output_dir: str) -> int:
    """从 MinIO 下载所有源文件到本地"""
    cfg = get_settings()
    client = Minio(
        cfg["endpoint"],
        cfg["access_key"],
        cfg["secret_key"],
        secure=cfg["secure"],
    )

    if not client.bucket_exists(cfg["bucket"]):
        print(f"[错误] 桶 '{cfg['bucket']}' 不存在")
        return 1

    # 列出所有 source.md 文件
    objects = client.list_objects(
        cfg["bucket"], prefix="documents/", recursive=True
    )
    sources = [o for o in objects if o.object_name.endswith("/source.md")]

    if not sources:
        print("[提示] 知识库中暂无文档")
        return 0

    os.makedirs(output_dir, exist_ok=True)
    count = 0

    for obj in sources:
        # 路径: documents/{dir_path}/{doc_id}/source.md
        # → 本地按 {dir_path}/{doc_id}.md 保存
        relative = obj.object_name[len("documents/"):]   # 去掉 documents/ 前缀
        parts = relative.split("/")
        doc_id = parts[-2]                                # UUID
        dir_path = "/".join(parts[:-2]) if len(parts) > 2 else ""

        # 构建本地路径
        local_dir = os.path.join(output_dir, dir_path) if dir_path else output_dir
        local_file = os.path.join(local_dir, f"{doc_id}.md")
        os.makedirs(local_dir, exist_ok=True)

        # 下载
        try:
            resp = client.get_object(cfg["bucket"], obj.object_name)
            content = resp.read()
            with open(local_file, "wb") as f:
                f.write(content)
            resp.close()
            resp.release_conn()
            count += 1
            print(f"  ✓ {local_file}")
        except Exception as e:
            print(f"  ✗ {obj.object_name}: {e}")

    print(f"\n[完成] 共备份 {count} 个文件到: {output_dir}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="知识库源文件备份工具")
    parser.add_argument(
        "--output", "-o",
        default=os.path.join(
            os.path.dirname(__file__), "..", "backups",
            f"kb-sources-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        ),
        help="备份输出目录 (默认: backups/kb-sources-<时间戳>)",
    )
    args = parser.parse_args()

    output_dir = os.path.abspath(args.output)
    print(f"备份目标: {output_dir}")
    print(f"MinIO 服务器: {os.environ.get('MINIO_ENDPOINT', 'localhost:9000')}")
    print()

    sys.exit(backup_sources(output_dir))


if __name__ == "__main__":
    main()

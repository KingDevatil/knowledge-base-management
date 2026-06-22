"""Command-line entry point for knowledge-base consistency diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from config import get_settings
from consistency import KnowledgeBaseConsistencyChecker
from knowledge_base import KnowledgeBase
from source_store import SourceStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check consistency between Redis document index, Chroma chunks, and source files."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a text report.",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Exit non-zero when warnings are present, not only errors.",
    )
    return parser


async def run_consistency_check() -> dict[str, Any]:
    import chromadb
    import redis.asyncio as redis

    settings = get_settings()
    redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        chroma = chromadb.HttpClient(host=settings.CHROMA_HOST, port=settings.CHROMA_PORT)
        kb = KnowledgeBase(chroma, settings.CHROMA_COLLECTION)
        kb.set_redis(redis_client)
        source_store = create_source_store(settings)
        checker = KnowledgeBaseConsistencyChecker(kb, source_store)
        return await checker.check()
    finally:
        await redis_client.close()


def create_source_store(settings: Any) -> Any:
    try:
        return SourceStore(
            endpoint=settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            bucket=settings.MINIO_BUCKET,
            secure=settings.MINIO_SECURE,
        )
    except Exception:
        from local_store import LocalFileStore

        fallback_base = (
            os.path.join(settings.KBDATA_DIR, "sources")
            if settings.KBDATA_DIR
            else "kbdata/sources"
        )
        return LocalFileStore(base_dir=fallback_base, bucket=settings.MINIO_BUCKET)


def format_text_report(result: dict[str, Any]) -> str:
    stats = result.get("stats", {})
    lines = [
        "Knowledge base consistency report",
        f"success: {result.get('success', False)}",
        f"indexed_documents: {stats.get('indexed_documents', 0)}",
        f"chroma_documents: {stats.get('chroma_documents', 0)}",
        f"errors: {stats.get('errors', 0)}",
        f"warnings: {stats.get('warnings', 0)}",
    ]

    issues = result.get("issues") or []
    if issues:
        lines.append("")
        lines.append("issues:")
        for issue in issues:
            doc_id = issue.get("doc_id") or "-"
            lines.append(
                f"- [{issue.get('severity')}] {issue.get('code')} doc_id={doc_id}: "
                f"{issue.get('message')}"
            )
    return "\n".join(lines)


def exit_code_for(result: dict[str, Any], fail_on_warning: bool = False) -> int:
    stats = result.get("stats", {})
    if stats.get("errors", 0) > 0:
        return 1
    if fail_on_warning and stats.get("warnings", 0) > 0:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = asyncio.run(run_consistency_check())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_text_report(result))
    return exit_code_for(result, fail_on_warning=args.fail_on_warning)


if __name__ == "__main__":
    raise SystemExit(main())

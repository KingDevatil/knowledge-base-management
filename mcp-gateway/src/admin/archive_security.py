"""Safe archive extraction helpers for admin uploads."""
from __future__ import annotations

import os
import re
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_FILES = 5000
MAX_MEMBER_BYTES = 50 * 1024 * 1024


class ArchiveValidationError(ValueError):
    """Raised when an uploaded archive is unsafe or exceeds limits."""


def validate_archive_size(content: bytes) -> None:
    if len(content) > MAX_ARCHIVE_BYTES:
        size_mb = len(content) // 1024 // 1024
        limit_mb = MAX_ARCHIVE_BYTES // 1024 // 1024
        raise ArchiveValidationError(f"压缩包过大（{size_mb}MB），最大支持 {limit_mb}MB")


def safe_extract_archive(archive_path: str, extract_dir: str, filename: str) -> int:
    """Safely extract a supported archive and return extracted file count."""
    lower_name = filename.lower()
    if lower_name.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            return _safe_extract_zip(zf, extract_dir)
    if lower_name.endswith(".tar.gz") or lower_name.endswith(".tgz"):
        with tarfile.open(archive_path, "r:gz") as tf:
            return _safe_extract_tar(tf, extract_dir)
    raise ArchiveValidationError("仅支持 .zip / .tar.gz")


def _safe_member_path(extract_dir: str, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        raise ArchiveValidationError(f"非法压缩包路径: {member_name}")
    if re.match(r"^[A-Za-z]:/", normalized):
        raise ArchiveValidationError(f"非法压缩包路径: {member_name}")

    parts = PurePosixPath(normalized).parts
    if any(part in ("", "..") for part in parts):
        raise ArchiveValidationError(f"压缩包路径不能包含上级目录: {member_name}")

    root = Path(extract_dir).resolve()
    target = (root / Path(*parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ArchiveValidationError(f"压缩包路径逃逸临时目录: {member_name}") from exc
    return target


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _safe_extract_zip(zf: zipfile.ZipFile, extract_dir: str) -> int:
    file_count = 0
    total_size = 0

    for info in zf.infolist():
        target = _safe_member_path(extract_dir, info.filename)
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if _is_zip_symlink(info):
            raise ArchiveValidationError(f"压缩包不能包含符号链接: {info.filename}")
        if info.file_size > MAX_MEMBER_BYTES:
            raise ArchiveValidationError(f"单个文件过大: {info.filename}")

        file_count += 1
        total_size += info.file_size
        _check_archive_limits(file_count, total_size)

        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info, "r") as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)

    return file_count


def _safe_extract_tar(tf: tarfile.TarFile, extract_dir: str) -> int:
    file_count = 0
    total_size = 0

    for member in tf.getmembers():
        target = _safe_member_path(extract_dir, member.name)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not member.isfile():
            raise ArchiveValidationError(f"压缩包不能包含非普通文件: {member.name}")
        if member.size > MAX_MEMBER_BYTES:
            raise ArchiveValidationError(f"单个文件过大: {member.name}")

        file_count += 1
        total_size += member.size
        _check_archive_limits(file_count, total_size)

        src = tf.extractfile(member)
        if src is None:
            raise ArchiveValidationError(f"无法读取压缩包文件: {member.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)

    return file_count


def _check_archive_limits(file_count: int, total_size: int) -> None:
    if file_count > MAX_ARCHIVE_FILES:
        raise ArchiveValidationError(f"压缩包文件数量过多，最多支持 {MAX_ARCHIVE_FILES} 个文件")
    if total_size > MAX_EXTRACTED_BYTES:
        limit_mb = MAX_EXTRACTED_BYTES // 1024 // 1024
        raise ArchiveValidationError(f"压缩包解压后内容过大，最大支持 {limit_mb}MB")

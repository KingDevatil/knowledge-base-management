"""Normalize uploaded document formats into Markdown for the ingestion pipeline."""

from __future__ import annotations

import csv
import io
import os
import re
from collections.abc import Sequence


SUPPORTED_DOCUMENT_EXTENSIONS = (".md", ".csv")


class DocumentConversionError(ValueError):
    """The uploaded document cannot be decoded or converted for ingestion."""


def is_supported_document_filename(filename: str | None) -> bool:
    return bool(filename) and os.path.splitext(filename)[1].lower() in SUPPORTED_DOCUMENT_EXTENSIONS


def convert_uploaded_document(filename: str, content: bytes) -> str:
    """Decode an uploaded Markdown or CSV file into Markdown ingestion content."""
    extension = os.path.splitext(filename)[1].lower()
    if extension not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise DocumentConversionError("仅支持 .md、.csv 文件")

    text = _decode_document_bytes(content)
    if extension == ".md":
        if not text.strip():
            raise DocumentConversionError("文档内容不能为空")
        return text
    return csv_to_markdown(text, title=_document_title(filename))


def csv_to_markdown(content: str, title: str) -> str:
    """Represent CSV rows as labelled Markdown records for reliable RAG retrieval."""
    rows = _read_csv_rows(content)
    if not rows:
        raise DocumentConversionError("CSV 文件没有可导入的数据行")

    has_header = _looks_like_header(rows)
    if has_header:
        headers = _normalise_headers(rows[0])
        data_rows = rows[1:]
    else:
        headers = _normalise_headers([f"列{index}" for index in range(1, max(map(len, rows)) + 1)])
        data_rows = rows

    records: list[str] = []
    for row_number, row in enumerate(data_rows, start=1):
        fields = []
        for index, raw_value in enumerate(row):
            value = _normalise_cell(raw_value)
            if not value:
                continue
            header = headers[index] if index < len(headers) else f"列{index + 1}"
            fields.append(f"  - {header}: {value}")
        if fields:
            records.append("\n".join([f"- 行号: {row_number}", *fields]))

    if not records:
        raise DocumentConversionError("CSV 文件没有可导入的数据行")

    clean_title = _normalise_cell(title) or "CSV 文档"
    return f"# {clean_title}\n\n## CSV 数据记录\n\n" + "\n\n".join(records) + "\n"


def _decode_document_bytes(content: bytes) -> str:
    if not content:
        raise DocumentConversionError("文档内容不能为空")

    encodings = ("utf-16",) if content.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" not in text:
            return text
    raise DocumentConversionError("编码错误，请使用 UTF-8、UTF-16 或 GB18030")


def _read_csv_rows(content: str) -> list[list[str]]:
    if not content.strip():
        return []
    sample = content[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    try:
        reader = csv.reader(io.StringIO(content), dialect=dialect, strict=True)
        return [row for row in reader if any(_normalise_cell(cell) for cell in row)]
    except csv.Error as exc:
        raise DocumentConversionError(f"CSV 格式错误：{exc}") from exc


def _looks_like_header(rows: Sequence[Sequence[str]]) -> bool:
    if len(rows) == 1:
        values = [_normalise_cell(value) for value in rows[0]]
        return bool(values) and all(value and not _is_number(value) for value in values)

    first_row = rows[0]
    following_rows = rows[1:]
    for index, first_value in enumerate(first_row):
        first_value = _normalise_cell(first_value)
        following_values = [
            _normalise_cell(row[index])
            for row in following_rows
            if index < len(row) and _normalise_cell(row[index])
        ]
        if _is_number(first_value) and following_values and all(_is_number(value) for value in following_values):
            return False

    # CSV exports normally have a header. Prefer it unless the first row clearly
    # looks like numeric data, so labels remain available in every retrieval chunk.
    return True


def _normalise_headers(headers: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: dict[str, int] = {}
    for index, raw_header in enumerate(headers, start=1):
        header = _normalise_cell(raw_header) or f"列{index}"
        count = seen.get(header, 0) + 1
        seen[header] = count
        result.append(header if count == 1 else f"{header}（{count}）")
    return result


def _normalise_cell(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _is_number(value: str) -> bool:
    if not value:
        return False
    try:
        float(value.replace(",", ""))
    except ValueError:
        return False
    return True


def _document_title(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return os.path.splitext(basename)[0]

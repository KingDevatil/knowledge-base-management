"""Rule-based metadata extraction from the short header of Markdown documents."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import unicodedata
from typing import Any, Iterable


HEADER_SCAN_MAX_LINES = 40
MAX_METADATA_VALUES = 32
MAX_METADATA_VALUE_CHARS = 120
_VALUE_SEPARATOR = re.compile(r"[，,、;；|]+")
_HEADER_FIELD = re.compile(r"^(?P<label>[^:：]{1,48})\s*[:：]\s*(?P<values>.+?)\s*$")
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+")
_SECONDARY_HEADING = re.compile(r"^#{2,}\s+")
_BULLET = re.compile(r"^[-*+]\s+")

_TAG_LABELS = {"标签", "標籤", "tag", "tags"}
_ENTITY_LABELS = {
    "核心实体",
    "核心實體",
    "实体",
    "實體",
    "core entity",
    "core entities",
    "entity",
    "entities",
}


@dataclass(frozen=True)
class DocumentHeaderMetadata:
    """Rule-extracted values declared in a document's short Markdown header."""

    tags: list[str]
    entities: list[str]


def extract_document_header_metadata(
    content: str,
    *,
    max_lines: int = HEADER_SCAN_MAX_LINES,
) -> DocumentHeaderMetadata:
    """Extract ``标签/Tags`` and ``核心实体/实体/Core Entities/Entities`` lines.

    The parser intentionally only examines the document header: the first ``max_lines``
    lines, stopping at a second-level section or code fence. It accepts Markdown quote
    prefixes, bold labels, ASCII/full-width colons, and common Chinese/English separators.
    List-item lines are deliberately ignored so CSV rows converted to Markdown are not
    accidentally treated as metadata.
    """

    tag_values: list[str] = []
    entity_values: list[str] = []
    if not content:
        return DocumentHeaderMetadata(tags=[], entities=[])

    for raw_line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n")[:max_lines]:
        stripped = raw_line.strip()
        if stripped.startswith(("```", "~~~")):
            break
        if _SECONDARY_HEADING.match(stripped):
            break

        line = _strip_header_prefix(stripped)
        if not line or _BULLET.match(line):
            continue
        match = _HEADER_FIELD.match(line)
        if not match:
            continue

        label = _normalize_label(match.group("label"))
        values = match.group("values")
        if label in _TAG_LABELS:
            tag_values.extend(_split_metadata_values(values))
        elif label in _ENTITY_LABELS:
            entity_values.extend(_split_metadata_values(values))

    return DocumentHeaderMetadata(
        tags=normalize_metadata_values(tag_values),
        entities=normalize_metadata_values(entity_values),
    )


def merge_metadata_values(*values: Any) -> list[str]:
    """Merge metadata lists using the same display-preserving de-duplication rules."""

    return normalize_metadata_values(values)


def normalize_metadata_values(values: Any) -> list[str]:
    """Normalize a scalar/list metadata value into a bounded, display-preserving list."""

    raw_values = _flatten_metadata_values(values)
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        for value in _split_metadata_values(raw_value):
            cleaned = _clean_metadata_value(value)
            if not cleaned:
                continue
            key = metadata_value_key(cleaned)
            if not key or key in seen:
                continue
            seen.add(key)
            normalized.append(cleaned)
            if len(normalized) >= MAX_METADATA_VALUES:
                return normalized
    return normalized


def metadata_value_key(value: str) -> str:
    """Return a conservative normalized key for equality and graph-node identity."""

    normalized = unicodedata.normalize("NFKC", value or "").casefold().strip()
    return re.sub(r"[\s_\-]+", "", normalized)


def entity_node_id(entity: str) -> str:
    """Return a stable opaque graph node id for an entity display name."""

    key = metadata_value_key(entity)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return f"entity:{digest}"


def metadata_override_enabled(value: Any) -> bool:
    """Return whether a persisted manual metadata override is active.

    Chroma and Redis normally preserve booleans, but old/manual data can contain
    a string or numeric value. Keep the conversion explicit so ``"false"`` is
    never treated as truthy by accident.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _strip_header_prefix(line: str) -> str:
    value = line
    while value.startswith(">"):
        value = value[1:].lstrip()
    return _MARKDOWN_HEADING.sub("", value, count=1)


def _normalize_label(label: str) -> str:
    value = unicodedata.normalize("NFKC", label or "")
    value = re.sub(r"[`*_]+", "", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def _flatten_metadata_values(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [values]
    if isinstance(values, Iterable):
        flattened: list[str] = []
        for item in values:
            flattened.extend(_flatten_metadata_values(item))
        return flattened
    return [str(values)]


def _split_metadata_values(value: str) -> list[str]:
    return _VALUE_SEPARATOR.split(value or "")


def _clean_metadata_value(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = normalized.strip(" \t`*_\"'“”‘’")
    normalized = re.sub(r"^#+\s*", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or len(normalized) > MAX_METADATA_VALUE_CHARS:
        return ""
    if not any(char.isalnum() for char in normalized):
        return ""
    return normalized
